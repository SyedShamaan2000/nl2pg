# How to run the Phase 3 baseline (Gemini 3.5-flash)

## Prerequisites
- `.env.example` copied to `.env` with `GEMINI_API_KEY=<key>`
- Packages installed (`uv pip install langchain-google-genai langchain langchain-community` done)
- Docker Postgres running (optional for baseline — baseline only calls LLM, it doesn't execute SQL by default)

## Run command (from repo root)
```bash
source .venv/bin/activate
python -m src.agent.baseline
```

## Enter the following requests (one per line, hit enter after each):
- `Show 10 recent customers`
- `Find pending orders`

## What input to provide
- Read requests: `Show all customers`, `Find pending orders`
- Write/ambiguous: `Update orders to refunded` (baseline will guess SQL without approval — this proves why approval gate is needed) - Now restricted to only SELECT queries, so it won't execute the write, but it will still generate a query for you to see.
- Hard: `Customers with more than 3 orders` (baseline has no schema context, likely wrong)
- - output for hard request:
```
(nl2pg) syed-work@Syeds-MacBook-Air nl2pg % python3 -m src.agent.baseline
Enter a natural-language request for SQL: Customers with more than 3 orders
INFO:google_genai.models:AFC is enabled with max remote calls: 10.
WARNING:google_genai.models:Direct use of automatic function calling (AFC) in Models.generate_content is not recommended. Instead, we recommend to use AFC in Chat.send_message. Similarly, direct use of AFC in Models.generate_content_stream is not recommended. Instead, we recommend to use AFC in Chat.send_message_stream.
INFO:httpx:HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent "HTTP/1.1 200 OK"
ERROR:__main__:DB execution failed: column c.customer_id does not exist
LINE 3: JOIN orders o ON c.customer_id = o.customer_id
                         ^
HINT:  Perhaps you meant to reference the column "o.customer_id".

{'raw_sql': 'SELECT c.customer_id, c.customer_name, COUNT(o.order_id) AS order_count\nFROM customers c\nJOIN orders o ON c.customer_id = o.customer_id\nGROUP BY c.customer_id, c.customer_name\nHAVING COUNT(o.order_id) > 3;', 'request': 'Customers with more than 3 orders', 'validated': False, 'approval_required': False, 'executed': False, 'rows': None, 'error': 'DB error: column c.customer_id does not exist\nLINE 3: JOIN orders o ON c.customer_id = o.customer_id\n                         ^\nHINT:  Perhaps you meant to reference the column "o.customer_id".\n'}
(nl2pg) syed-work@Syeds-MacBook-Air nl2pg % 
```