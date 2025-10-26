import os, anthropic

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")      # читает из .env
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

MODEL = "claude-3-haiku-20240307"                   # самая дёшевая модель

async def generate_cover_letter(vacancy: str, resume: str) -> str:
    """
    vacancy – текст/описание вакансии
    resume  – твой шаблон резюме (или summary кандидата)
    """
    prompt = (
        "Ты – HR-ассистент. На основе описания вакансии и резюме кандидата "
        "напиши короткое (до 120 слов) сопроводительное письмо, подчеркивая "
        "релевантный опыт и мотивированность.\n\n"
        f"Описание вакансии:\n{vacancy}\n\n"
        f"Резюме кандидата:\n{resume}"
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        temperature=0.3,
        messages=[{"role": "user", "content": prompt}],
    )

    # resp.content = [{'text': '...', 'type': 'text'}]
    return resp.content[0]["text"].strip()
