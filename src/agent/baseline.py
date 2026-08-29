"""Baseline: naive single-prompt LLM approach (deliberately weak).

Executes only safe SELECTs. Any statement containing DELETE/DROP/TRUNCATE/ALTER
/UPDATE/INSERT is refused — Ground Rule #4 forbids auto-execution of writes.
"""

import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from src.db.connection import get_connection

load_dotenv()

logger = logging.getLogger(__name__)

# Statements the baseline will refuse to execute. The regex matches the
# keyword as a whole word, case-insensitive, so it does not match e.g. a
# column literally named "updated_at". This is the baseline's only safety
# check — the real agent does this properly via schema validation + approval.
BLOCKED_KEYWORDS = ("delete", "drop", "truncate", "alter", "update", "insert")
BLOCKED_PATTERN = re.compile(r"\b(" + "|".join(BLOCKED_KEYWORDS) + r")\b", re.IGNORECASE)


class BaselineAgent:
    """Simple LLM agent with minimal context — deliberately naive."""

    def __init__(self) -> None:
        api_key = os.getenv("GEMINI_API_KEY") or ""
        if not api_key:
            logger.warning("GEMINI_API_KEY not set; baseline LLM calls will fail.")
        kwargs: dict[str, object] = {"model": "gemini-3.5-flash", "temperature": 0.7}
        if api_key:
            kwargs["google_api_key"] = api_key
        self.llm = ChatGoogleGenerativeAI(**kwargs)  # type: ignore[arg-type]
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "You generate Postgres SQL. Do not explain. Return only SQL."),
                ("human", "Request: {request}"),
            ]
        )

    def _extract_text(self, response: object) -> str:
        """Pull the assistant text out of a Gemini response.

        Gemini via langchain-google-genai returns a list of typed content parts
        (e.g. [{'type': 'text', 'text': '...'}]) rather than a plain string.
        Fall back to str() so callers always get something renderable.
        """
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if text:
                        parts.append(text)
                else:
                    text = getattr(part, "text", None)
                    if text:
                        parts.append(text)
            return "\n".join(parts) if parts else str(content)
        return str(content)

    def run(self, request: str) -> dict[str, Any]:
        if not request or not request.strip():
            logger.warning("Baseline received empty request")
            return {
                "raw_sql": None,
                "request": request,
                "validated": False,
                "approval_required": False,
                "executed": False,
                "error": "Empty natural-language request.",
            }
        logger.debug(f"Baseline received request: {request!r}")

        try:
            chain = self.prompt | self.llm
            response = chain.invoke({"request": request})
            raw_text = self._extract_text(response)
            logger.debug(f"Baseline raw LLM response: {raw_text!r}")
        except Exception as exc:
            logger.error(f"LLM call failed: {exc}")
            raw_text = self._guess_sql(request)
            logger.debug(f"Baseline fallback SQL: {raw_text!r}")

        raw_sql = self._clean_sql(raw_text)
        logger.debug(f"Baseline cleaned SQL: {raw_sql!r}")

        safety = self._check_safety(raw_sql)
        if safety is not None:
            logger.warning(f"Baseline refused to execute: {safety}")
            return {
                "raw_sql": raw_sql,
                "request": request,
                "validated": False,
                "approval_required": False,
                "executed": False,
                "error": safety,
            }

        rows, error = self._execute_select(raw_sql)
        return {
            "raw_sql": raw_sql,
            "request": request,
            "validated": False,
            "approval_required": False,
            "executed": error is None,
            "rows": rows,
            "error": error,
        }

    def _clean_sql(self, text: str) -> str:
        """Strip markdown fences and whitespace from LLM text."""
        cleaned = text.strip()
        for fence in ("```sql", "```sql\n", "```", "\n```"):
            cleaned = cleaned.replace(fence, "")
        return cleaned.strip()

    def _check_safety(self, sql: str) -> str | None:
        if BLOCKED_PATTERN.search(sql):
            return (
                "Refused: destructive or write operation detected. "
                "Baseline never executes DELETE/DROP/TRUNCATE/ALTER/UPDATE/INSERT."
            )
        # Require SELECT for execution; deny anything that isn't clearly a read.
        first_token = sql.lstrip().split()[0].upper() if sql.lstrip() else ""
        if first_token != "SELECT":
            return f"Refused: baseline only executes SELECT statements (got '{first_token}')."
        return None

    def _execute_select(self, sql: str) -> tuple[list[dict[str, Any]] | None, str | None]:
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(sql)
                if cur.description is None:
                    conn.commit()
                    return None, "Query executed but returned no rows."
                cols = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                results = [dict(zip(cols, row)) for row in rows]
                logger.info(f"Baseline executed SELECT; {len(results)} row(s) returned")
                return results, None
        except Exception as exc:
            logger.error(f"DB execution failed: {exc}")
            return None, f"DB error: {exc}"

    def _guess_sql(self, request: str) -> str:
        """Naive keyword-based SQL fallback used only when the LLM call fails.

        Kept intentionally weak — it never inspects the database schema.
        """
        request_lower = request.lower()
        if "customer" in request_lower:
            return "SELECT * FROM customers;"
        elif "order" in request_lower:
            return "SELECT * FROM orders;"
        elif "pending" in request_lower:
            return "SELECT * FROM orders WHERE status = 'pending';"
        else:
            return "SELECT * FROM orders;"


# main call for running the baseline agent directly (for testing)
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent = BaselineAgent()
    request = input("Enter a natural-language request for SQL: ")
    result = agent.run(request)
    print(result)
