# Putting the security boundary in the database

[![CI](https://github.com/matuck40/db-enforced-agent-permissions/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/matuck40/db-enforced-agent-permissions/actions/workflows/ci.yml)

### What I learned shipping an LLM ordering agent for a real restaurant

**What is in this repository.** A self-contained reimplementation of the permission layer of a client system, not a copy of it: the Postgres schema with row-level security on every table (`schema.sql`), one SQL test suite per database role that exercises the policies from that role's position, a regression test for the bug described below (inside the floor staff suite), a test that the agent role is refused outside its grant (inside the agent suite), and a `docker-compose.yml` that brings up a database to run all of it against. `./run-tests.sh` starts the container and runs everything.

What changed on the way from the client system to this repository: each post is a Postgres login here, where the client system carries the role through Supabase Auth and organization memberships; the organization dimension is gone; eleven roles became five; the agent's grant is narrower (see below); and the review that preceded publication added a few things the client system does not have yet, such as guards against `NaN` in money and stock. Names, values and business terms were changed. The policies and the immutability rules were kept.

I built and deployed a management system with an LLM attendant on WhatsApp for a client. Customers order in a chat; staff run the restaurant in the same system. It is installed on site; a staff pilot has not been scheduled yet.

This is a write-up of the one decision that mattered, the bug that proved it was the right decision, and what I would do differently.

---

## The first version worked, and I threw the architecture away

The prototype was the obvious stack: n8n for orchestration, Evolution API for the WhatsApp bridge, and an OpenAI model behind it. It took orders. It worked well enough to demo.

I retired the architecture for two reasons, and neither was about model quality. The bridge and the model stayed; what moved was where the credentials and the permissions live.

**The credentials lived in `.env`.** Every component that could read the environment could reach every service. There was no separation between "the thing that answers a customer" and "the thing that can touch the till".

**The agent's reach was the whole system.** The model had tools, and the tools had the permissions of the process running them. Nothing in the architecture said *what an attendant is not allowed to do*. The only thing standing between a confused model and a deleted order was the prompt. A prompt is not a permission system.

That is fine for a demo and unacceptable for a business that takes money.

---

## The rebuild: the boundary moved into the database

The second version starts from a different question. Not "what tools should the agent have", but **"what is this actor allowed to do, enforced somewhere the agent cannot argue with"**.

The answer was row-level security on every table, with roles that mirror the real restaurant.

**Roles that map to posts, not to an abstract tree.** The client system has eleven roles. Five of them are physical posts: the counter, the kitchen, service, the cashier, the scale. The rest are the owner, management, back office, stock, audit, and the agent. If a person cannot see something from where they stand in the restaurant, their role cannot see it in the database either. Modelling the roles on the physical workflow rather than on an abstract permission tree turned out to be the decision that made everything else legible. This repository collapses the eleven to five (floor staff, kitchen, cashier, manager, ordering agent) plus an owner login that holds the tables and has no post.

**The LLM connector is its own role, and it is the weakest one.** In the client system the WhatsApp and model integration runs under a dedicated database role that can create an order and nothing else; even cancellation was removed from it. It can read the order history of the phone it is talking to. It cannot touch payments. It cannot adjust stock. If the model is confused, jailbroken, or simply wrong, the blast radius is one order. In this repository the grant is narrower still: create an order, submit it, and read back the one order it created, by id. That is the version I would ship next.

This is the part I would argue for in any agentic system. **The agent's permissions should be enforced by the layer below it, not described in the layer above it.** Prompts are guidance. Row-level security is a rule.

**Payments and stock are immutable.** There is no `UPDATE` on a payment and no `DELETE` on a stock movement. Corrections happen by counter-entry, the way accounting has done it for centuries. This costs a little convenience and buys an auditable history that cannot be quietly rewritten, by a human or by a model.

**Nobody bypasses the policies, including the owner.** The owner is not exempt from row-level security; the owner is simply on every policy's allow list. That distinction matters for the bug below.

The client system is TypeScript with the Next.js App Router (running on Vite and Cloudflare Workers through vinext) over Postgres, and the schema arrived at its current shape across 105 migrations.

---

## The bug that proved the design

The scale role could not see the orders entered at its own post.

The original policy was deliberate and matched the spec: the scale sees only the orders that arrived through the chat. Then the workflow changed. The scale post also became the counter and started entering orders itself, and the policy did not change with it. From that day the role could see every order that came in through the chat and none of the orders it had just typed in. Nothing failed loudly. The rows simply were not there.

It was caught the next day, and only because a test account with that role existed. Testing as the owner would never have shown it: the owner passes every check, so every screen looks right from there, which is a position no real user occupies.

Two rules came out of it, and I now apply both without exception. **A policy encodes an assumption about the workflow, and when the workflow changes the policy keeps the old assumption silently**, so every change to how a post works is a change to its policies. And **test with a real account of the role, never with an admin account.** If your permission model is enforced in the database, then any test that runs as the owner is not testing your permission model. It is testing a different system that happens to share a schema.

In this repository the regression test lives in the floor staff suite: orders from the `chat` channel and orders entered at the post must both be visible to the post.

This generalises past databases. It is the same mistake as evaluating a model on data it has effectively already seen: you are measuring something real, it just is not the thing you think you are measuring.

---

## How it was built

More than 500 commits, written largely with Claude Code, driven against a `CLAUDE.md` that carries the project's conventions, the schema's invariants, and the things the agent must not do.

I want to be precise about what that does and does not mean, because "I use AI to code" is a claim every candidate makes and every reader discounts.

What it bought: speed on the mechanical layers, migrations, CRUD, the shape of components, test scaffolding.

What it did not buy: the architecture. No agent proposed moving the boundary into the database. That came from looking at the first version and being unhappy with it. The agent was very good at building what I described and completely uninterested in whether it was the right thing.

The `CLAUDE.md` is the actual artefact worth talking about. It is where the invariants live: what must never be mutable, which role may write which table, what an attendant is not allowed to do. Once those were written down as rules rather than held in my head, the agent stopped producing code that violated them. The instruction file became the place where the design decisions are enforced, in the same way the RLS policies are where the permissions are enforced.

---

## What I would do differently

**Testing like a real customer, from day one.** I ended up assessing the attendant with manual tests on a real phone, and real bugs turned into end-to-end regression tests, because synthetic cases passed and conversations on a real phone broke. Real users are ungrammatical, change their minds mid-sentence, and ask for things that are not on the menu. I got there eventually; I should have started there.

**I do not have formal agent evaluation at scale.** Curated datasets, scorers, judges, regression suites across versions. What I have is manual tests on a real phone and end-to-end regression tests built from real bugs, in a small system, and the knowledge that the synthetic version lied to me. That is a gap and I would rather say so than imply otherwise.

---

## Where this comes from

I am an engineering physicist by training. My doctorate was on estimating the internal state of battery cells, internal temperature and internal pressure, from signals measurable outside the cell, validated against the internal quantity physically measured rather than against a simulation, with the data split by cycle so that no cycle appeared in both training and test.

Different domain, same discipline: decide what your ground truth actually is, design the split so you are not fooling yourself, and say out loud where the method stops working.

The restaurant system is the same instinct applied to a different problem. Decide what the actor is actually allowed to do, enforce it somewhere it cannot be talked out of, and test from the position a real user occupies.

---

*The client system's full repository is private. This repository is the part that can be shown; I am happy to walk through the rest. Other public repositories: `rag-chatbot`, `probabilistic-load-forecast`.*
