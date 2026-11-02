"""Balance guard for callers preparing a batch (no queue implemented)."""
from decimal import Decimal, InvalidOperation
from typing import Any
from openai import OpenAI
from .client import BASE_URL, get_api_key, with_retries
from .errors import BalanceError, ResponseError


def check_balance(client: OpenAI | None = None) -> Decimal:
    owned = client is None
    sdk = client or OpenAI(api_key=get_api_key(), base_url=BASE_URL, max_retries=0, timeout=30)
    sdk.max_retries = 0
    try:
        data = with_retries(lambda: sdk.get("/user/balance", cast_to=dict[str, Any]))
        if not isinstance(data, dict):
            raise ResponseError("Некорректный ответ проверки баланса.")
        if data.get("is_available") is False:
            raise BalanceError("DeepSeek недоступен по балансу: пополни баланс.")
        if data.get("is_available") is not True:
            raise ResponseError("Некорректный ответ проверки баланса.")
        try:
            balances = [Decimal(row["total_balance"]) for row in data["balance_infos"] if row["currency"] == "USD"]
            if not balances or any(not amount.is_finite() for amount in balances):
                raise ValueError("missing USD balance")
            return sum(balances, Decimal(0))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ResponseError("DeepSeek не вернул корректный баланс в USD.") from None
    finally:
        if owned:
            sdk.close()


def guard_balance(client: OpenAI | None = None) -> Decimal:
    balance = check_balance(client)
    if balance < Decimal("0.5"):
        raise BalanceError("Для пакетной обработки нужно минимум $0.50: пополни баланс.")
    return balance
