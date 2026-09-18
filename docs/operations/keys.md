# Consumer keys

Create one LiteLLM virtual key per consumer before sharing gateway access. The master
key bypasses budgets; keep it for administration.

## API

Use the LiteLLM hostname from the Stack Console. Set `LITELLM_URL` (for example
`http://litellm.localhost`), load `LITELLM_MASTER_KEY` securely from the protected `.env`,
and avoid shell tracing. This example allows only the configured `gateway-mock` alias,
with a $25 budget per 30-day reset period, 60 requests/minute and 100000 tokens/minute.
Replace the model list with the consumer's allowed aliases from `config.yaml`.

```sh
umask 077
key_work=$(mktemp -d)
trap 'rm -rf "$key_work"' EXIT HUP INT TERM
printf 'Authorization: Bearer %s\n' "$LITELLM_MASTER_KEY" > "$key_work/admin-header"
curl --fail-with-body -sS "$LITELLM_URL/key/generate" \
  --header "@$key_work/admin-header" \
  -H 'Content-Type: application/json' \
  -d '{"key_alias":"consumer-example","models":["gateway-mock"],"max_budget":25,"budget_duration":"30d","rpm_limit":60,"tpm_limit":100000}' \
  > consumer-key.json
```

Run these examples in the same shell so the protected temporary files remain available.
Credentials stay out of curl arguments; same-user processes can still read the environment
and temporary files. The trap removes the temporary directory when the shell exits.

Check for a successful response. Save its `key` in the consumer's secret store, deliver it
securely, then remove the local response file. Consumers send that key as the Bearer token.
A reset period differs from expiration; use `duration` separately for an expiring key.
Budgets depend on reported model costs and persisted spend; the mock model has no real
provider spend, so it cannot prove a dollar budget. Concurrent requests and accounting
latency can overshoot the configured amount. Keep provider-side limits too.

Set `CONSUMER_KEY` privately to inspect its `info.spend`, `max_budget`, `budget_duration`,
`budget_reset_at`, model list and rate limits:

```sh
printf %s "$CONSUMER_KEY" > "$key_work/consumer-key"
curl --fail-with-body -sS --get "$LITELLM_URL/key/info" \
  --header "@$key_work/admin-header" \
  --data-urlencode "key@$key_work/consumer-key"
```

Key info can contain sensitive fields; do not paste the response into public logs.
Revoke with `/key/delete`, then verify a request using the old key is rejected:

```sh
python3 -c 'import json, os; print(json.dumps({"keys":[os.environ["CONSUMER_KEY"]]}))' |
  curl --fail-with-body -sS "$LITELLM_URL/key/delete" \
    --header "@$key_work/admin-header" \
    -H 'Content-Type: application/json' --data-binary @-
```

Export `CONSUMER_KEY` for the Python command. If a revoked key remains usable, check
cache invalidation and test again before declaring revocation complete.

## UI

From a source in `LG_OPERATOR_ALLOW`, open `litellm.<domain>/ui` and sign in with
`UI_USERNAME` (generated as `admin`) and the generated `UI_PASSWORD` from `.env`.
The master key is never used in a browser. Select **Virtual Keys → Create New Key**. Name the consumer, select its
allowed models, and set **Max Budget**, **Budget Duration**, **RPM Limit** and **TPM Limit**
in the optional settings. Save and store the displayed key securely. Open that key's
details to check **Spend**, limits and reset time; use its delete action to revoke it.
Check `/key/info` if the UI labels differ in a later pin.

Upstream: [virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys),
[budgets and rate limits](https://docs.litellm.ai/docs/proxy/users),
[UI key creation](https://docs.litellm.ai/docs/proxy/docker_quick_start).
