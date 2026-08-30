"""Baseline: naive single-prompt LLM approach (deliberately weak).

Executes only safe SELECTs. Any statement containing DELETE/DROP/TRUNCATE/ALTER
/UPDATE/INSERT is refused — Ground Rule #4 forbids auto-execution of writes.

Provider notes:
  - Defaults to Groq (matches the main agent, no Gemini calls / 401 spam).
  - Gemini code is kept intact and can be re-enabled by setting
    BASELINE_LLM_PROVIDER=gemini (or passing provider="gemini" directly)
    — no code changes needed to switch back.

Rate limiting: LLM calls go through `src.agent.rate_limit.invoke_with_backoff`,
which paces requests process-wide and retries 429s / transient Groq errors
with backoff. See that module's docstring for details — this matters here
because the eval harness runs baseline and agent back-to-back per case, so
both hit the same provider rate limit.
"""

import logging
import os
import re
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

from src.agent.rate_limit import invoke_with_backoff
from src.db.connection import get_connection

load_dotenv()

logger = logging.getLogger(__name__)

# Statements the baseline will refuse to execute. The regex matches the
# keyword as a whole word, case-insensitive, so it does not match e.g. a
# column literally named "updated_at". This is the baseline's only safety
# check — the real agent does this properly via schema validation + approval.
BLOCKED_KEYWORDS = ("delete", "drop", "truncate", "alter", "update", "insert")
BLOCKED_PATTERN = re.compile(r"\b(" + "|".join(BLOCKED_KEYWORDS) + r")\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# LLM Initializer Functions (mirrors src/agent/agent.py so both agents pick
# providers the same way)
# ---------------------------------------------------------------------------


def _init_gemini(
    model_name: str = "gemini-3.5-flash", temperature: float = 0.7
) -> ChatGoogleGenerativeAI:
    """Initialize and return a Gemini LLM instance."""
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    if not api_key:
        logger.warning("GOOGLE_API_KEY / GEMINI_API_KEY not set; Gemini LLM calls will fail.")

    return ChatGoogleGenerativeAI(
        model=model_name,
        temperature=temperature,
        google_api_key=api_key if api_key else None,
    )


def _init_groq(model_name: str = "openai/gpt-oss-120b", temperature: float = 0.7) -> ChatGroq:
    """Initialize and return a Groq LLM instance."""
    api_key = os.getenv("GROQ_API_KEY") or ""
    if not api_key:
        logger.warning("GROQ_API_KEY not set; Groq LLM calls will fail.")

    return ChatGroq(
        model_name=model_name,
        temperature=temperature,
        groq_api_key=api_key if api_key else None,
    )


def get_llm(
    provider: Literal["groq", "gemini"] = "groq",
    model_name: str | None = None,
    temperature: float = 0.7,
) -> BaseChatModel:
    """Factory function to instantiate an LLM based on provider choice."""
    if provider == "groq":
        target_model = model_name or "openai/gpt-oss-120b"
        return _init_groq(model_name=target_model, temperature=temperature)
    elif provider == "gemini":
        target_model = model_name or "gemini-3.5-flash"
        return _init_gemini(model_name=target_model, temperature=temperature)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}. Choose 'groq' or 'gemini'.")


class BaselineAgent:
    """Simple LLM agent with minimal context — deliberately naive.

    Provider defaults to Groq (via BASELINE_LLM_PROVIDER env var, falling
    back to "groq"). Pass provider="gemini" explicitly, or set
    BASELINE_LLM_PROVIDER=gemini in .env, to switch back to Gemini —
    that code path is untouched, just not the default anymore.
    """

    def __init__(
        self,
        provider: Literal["groq", "gemini"] | None = None,
        model_name: str | None = None,
    ) -> None:
        selected_provider = provider or os.getenv("BASELINE_LLM_PROVIDER", "groq").lower()  # type: ignore[assignment]
        self.provider = selected_provider
        self.llm = get_llm(
            provider=selected_provider,
            model_name=model_name,
            temperature=0.7,
        )
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "You generate Postgres SQL. Do not explain. Return only SQL."),
                ("human", "Request: {request}"),
            ]
        )

    def _extract_text(self, response: object) -> str:
        """Pull the assistant text out of an LLM response.

        Gemini via langchain-google-genai returns a list of typed content parts
        (e.g. [{'type': 'text', 'text': '...'}]) rather than a plain string.
        Groq (OpenAI-style) typically returns a plain string. Handle both,
        falling back to str() so callers always get something renderable.
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
        logger.debug(f"Baseline ({self.provider}) received request: {request!r}")

        try:
            chain = self.prompt | self.llm
            response = invoke_with_backoff(chain.invoke, {"request": request})
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

    # Defaults to Groq. Uncomment to test Gemini instead:
    # agent = BaselineAgent(provider="gemini")
    agent = BaselineAgent(provider="groq")

    request = input("Enter a natural-language request for SQL: ")
    result = agent.run(request)
    print(result)
