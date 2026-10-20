# db-enforced-agent-permissions

The permission layer of an ordering system with an LLM chat agent, enforced in
Postgres. The model proposes; the database decides. `README.md` is the public
argument for this design: never change code in a way that contradicts it, and
never add a claim to it that the code or a recorded measurement does not back.

## Roles

| Role             | Post                    | May write through                                   |
|------------------|-------------------------|-----------------------------------------------------|
| `floor_staff`    | counter and service     | `create_order`, `advance_order`                     |
| `kitchen`        | kitchen                 | `advance_order`, stock movements                    |
| `cashier`        | till                    | `create_order`, `advance_order`, payments           |
| `manager`        | management              | everything above                                    |
| `ordering_agent` | the LLM chat agent      | `create_order` (chat only), `advance_order` (draft to submitted only) |
| `app_owner`      | none: owns the schema   | nothing at runtime                                  |

- Every role above except `app_owner` is a login and a member of `app_user`;
  its station comes from `app.staff`, looked up by `session_user`.
- `app_owner` owns the tables and is the identity the `SECURITY DEFINER`
  functions run as. It has `BYPASSRLS` and `NOLOGIN`. Neither the application
  nor the agent connects as it; only `tests/setup.sql` turns its login on, so
  that `tests/60_app_owner.sql` can prove the append-only triggers hold even
  for it.

## Invariants

- Row-level security is enabled and forced on every table.
- No role has a direct write grant on any table. Every write is a
  `SECURITY DEFINER` function in `app` that checks the caller's station itself,
  with `set search_path = ''`.
- For catalog items the price always comes from `app.menu_items`; a price sent
  by the caller of `create_order` is ignored. Only `floor_staff`, `cashier`
  and `manager` may add an open item with a price of their own; the agent may
  not.
- Payments and stock movements are append-only. Corrections are reversing
  entries. Every numeric column rejects `NaN`.
- Station checks use `session_user`, never `current_user`.

## What the ordering agent can never do

Its whole grant is: read the menu, create a chat order from catalog items (as
a draft, whole quantities from 1 to 20), read that order's status and total by
id through `app.chat_order_summary`, and confirm it (draft to submitted), all
within one conversation.

The conversation scope (`migrations/001_conversation_scope.sql`): the harness
generates a token per conversation (`secrets.token_urlsafe(32)`) and `AgentDB`
sets it with `set_config('app.conversation', token, true)` as the first
statement of every transaction, so it never outlives the transaction. The
database stores only its sha256 (pgcrypto, in schema `extensions`) in
`app.orders.conversation_hash`, null for orders the agent did not create. For
the agent, `create_order` refuses a token shorter than 32 characters (42501),
and `chat_order_summary` and `advance_order` see only chat orders whose hash
matches the current token; anything else is P0002, like an order that does not
exist. A null hash never matches, so chat orders created before the migration
are out of the agent's reach. The token is never logged, never put in an
exception, the prompt, the graph state or a report.

It cannot: list or read orders through the tables (the hash column included),
read or confirm a chat order of another conversation even with its id, create
an order without a conversation token, address an order from another channel,
cancel or advance an order past submitted, order an item that is not in the
catalog or set its price, record or read payments, read or change stock, read
staff, switch to another role (including `app_owner`), create objects, or
write any table with raw SQL. `tests/40_ordering_agent.sql` checks each of
these from the agent's own login.

Never widen the agent's grant to make a feature work. Change the feature.

## Tests

```
./run-tests.sh                 # fresh Postgres in Docker, every suite, teardown
KEEP_DB=1 ./run-tests.sh       # leave it up on 127.0.0.1:55432
EXTERNAL_DB=1 PGHOST=... PGPORT=... ./run-tests.sh   # a fresh database already running (CI)
```

- The current schema is `schema.sql` plus `migrations/*.sql` in name order.
  `schema.sql` is never edited: it stays the schema from before the first
  migration, and a fix is a new migration with its tests. Every loader applies
  them in that order: `run-tests.sh`, `agent/tests/conftest.py`, and the eval
  runner (`--schema schema.sql` then one `--schema` per `migrations/*.sql`, as
  CI builds it with a glob; `--schema schema.sql` alone reproduces the run
  before the fix).
- `run-tests.sh` loads `schema.sql`, then `tests/legacy_orders.sql` (rows that
  exist before the migrations, as the superuser), then the migrations, then
  `tests/setup.sql`, so the suites see what a migration does to existing rows.
- One file per role in `tests/`, declaring `-- role: <login>` on its first
  lines. It runs connected as that login, never as a superuser, and must end
  with `\echo 'OK: <role>'`.
- Test every policy from the position of the role it is about. A test that runs
  as the owner or a superuser does not test the permission model.
- A change to how a post works is a change to its policies: update the
  policy and that role's suite together.

## Conventions

- Code, comments, commit messages and the README are in English.
- No secrets in the repository. Credentials only through environment
  variables.
- Commits are small. CI (`.github/workflows/ci.yml`) runs the suites and
  gitleaks on every push to `main` and every pull request.
- Personal working notes go in `CLAUDE.local.md`, which git ignores.
