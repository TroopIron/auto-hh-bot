import os
import httpx
from typing import Any, Dict, List


class HHApiClient:
    # Константы API
    BASE_URL = "https://api.hh.ru"
    AUTH_URL = "https://hh.ru/oauth/authorize"  # всегда hh.ru
    TOKEN_URL = "https://hh.ru/oauth/token"

    def __init__(self, token: str | None = None):
        """Базовая инициализация клиента HH API."""
        self._headers = {
            "User-Agent": os.getenv(
                "HH_USER_AGENT", "HH HunterBot/1.0 (tg:@your_nick)"
            )
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url="https://api.hh.ru", headers=self._headers, timeout=15
        )

    async def exchange_code_for_token(self, code: str) -> Dict[str, Any]:
        """Обменивает authorization code на пару токенов."""
        data = {
            "grant_type": "authorization_code",
            "client_id": os.getenv("HH_CLIENT_ID"),
            "client_secret": os.getenv("HH_CLIENT_SECRET"),
            "code": code,
            "redirect_uri": os.getenv("REDIRECT_URI"),
        }
        resp = await self._client.post(self.TOKEN_URL, data=data)
        if resp.status_code != 200:
            # Логируем код и тело ответа
            import logging
            logger = logging.getLogger(__name__)
            logger.error("HH OAuth error %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()
        return resp.json()

    async def search_vacancies(
        self,
        text: str,
        per_page: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Поиск вакансий по тексту.
        """
        params = {"text": text, "per_page": per_page}
        resp = await self._client.get(
            f"{self.BASE_URL}/vacancies",
            params=params,
        )
        resp.raise_for_status()
        return resp.json().get("items", [])

    async def list_resumes(self) -> List[Dict[str, Any]]:
        """
        Получение списка резюме пользователя.
        """
        resp = await self._client.get(f"{self.BASE_URL}/resumes/mine")
        resp.raise_for_status()
        return resp.json().get("items", [])

    async def get_vacancy(self, vacancy_id: str) -> Dict[str, Any]:
        """
        Получение детальной информации о вакансии по ID.
        """
        resp = await self._client.get(f"{self.BASE_URL}/vacancies/{vacancy_id}")
        resp.raise_for_status()
        return resp.json()

    async def respond_to_vacancy(
        self,
        vacancy_id: str,
        resume_id: str,
        cover_letter: str,
    ) -> Dict[str, Any]:
        """
        Отправка отклика на вакансию (создание переговоров).
        """
        payload = {
            "vacancy_id": vacancy_id,
            "resume_id": resume_id,
            "cover_letter": cover_letter,
        }
        resp = await self._client.post(
            f"{self.BASE_URL}/negotiations",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

        # ─── резюме целиком ─────────────────────────────────────
    async def get_resume_text(self, resume_id: str) -> str:
        """
        Возвращает «плоский» текст резюме (заголовок + ключ. навыки + опыт),
        чтобы скормить его Claude-у для сопроводительного письма.
        """
        url = f"{self.BASE_URL}/resumes/{resume_id}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()

        parts: list[str] = [data.get("title", "")]

        # ключевые навыки
        skills = ", ".join([s["name"] for s in data.get("key_skills", [])])
        if skills:
            parts.append(f"Навыки: {skills}")

        # опыт работы (берём только заголовки компаний и позиций)
        for exp in data.get("experience", []):
            comp = exp.get("company", "")
            pos  = exp.get("position", "")
            parts.append(f"{pos} — {comp}")

        return "\n".join(filter(None, parts))


    async def close(self):
        """
        Закрывает HTTP-сессию.
        """
        await self._client.aclose()


class AreaSuggestion:
    def __init__(self, name: str, id: str):
        # текстовое название региона и его внутренний идентификатор
        self.name = name
        self.id = id

async def get_area_suggestions(query: str) -> List[AreaSuggestion]:
    """
    Делает запрос к HH API /suggests/areas?text=<query>
    и возвращает список похожих локаций.
    """
    url = "https://api.hh.ru/suggests/areas"
    params = {"text": query}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
    items = data.get("items", [])
    # из каждого элемента берём 'text' (имя) и 'id'
    return [AreaSuggestion(item["text"], item["id"]) for item in items]


# общий клиент HH API для простых запросов
_ua = os.getenv("HH_USER_AGENT", "HH HunterBot/1.0 (tg:@your_nick)")
client = httpx.AsyncClient(
    base_url="https://api.hh.ru", timeout=5.0, headers={"User-Agent": _ua}
)


async def area_name(area_id: str | int | None) -> str:
    """Возвращает человекочитаемое название области HH."""
    if not area_id:
        return "—"
    if not str(area_id).isdigit():
        return str(area_id)
    try:
        async with client.get(f"/areas/{area_id}") as resp:
            if resp.status_code == 200:
                data = await resp.json()
                return data.get("name") or str(area_id)
    except Exception:
        pass
    return str(area_id)