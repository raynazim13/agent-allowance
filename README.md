# Agent Allowance

An AI restocking agent for small businesses that can only spend inside
rules the owner sets. A plain-code policy engine (not the AI) returns
**AUTO / HOLD / BLOCK** for every order. Built for the PayPal AI Hackathon.

## Run it (VS Code terminal)
```
python -m venv .venv
.venv\Scripts\activate        # Windows  (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env        # Mac/Linux: cp .env.example .env
pytest                        # should show 17 passed
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000/docs and try the endpoints in order:
1. `GET /state` - inventory, mandate, approval queue, audit log
2. `POST /agent/run` - agent restocks low items (AUTO / HOLD / BLOCK)
3. `POST /purchases/{id}/approve` or `/deny` - handle held orders
4. `POST /mandate` with `{"active": false}` - kill switch
5. `POST /advance-day` - simulate a day of sales
6. `POST /reset` - reload demo data

## How it works
- `app/policy.py` - the rules. Hard limits BLOCK, soft limits HOLD.
- `app/service.py` - order flow; prices come from the supplier catalogue,
  never from the AI.
- `app/agent.py` - LLM with tools, or a rule-based planner if no key.
- `app/payments.py` - `FakePayments` now; `PayPalPayments` is the stub to
  fill in with PayPal sandbox calls.

## Status
- [x] Policy engine + tests, order flow, agent, API, audit log
- [ ] PayPal sandbox payments (`PayPalPayments`)
- [ ] Web UI (mandate, dashboard, approval queue, audit log)

## License
MIT
