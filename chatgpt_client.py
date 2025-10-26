import os

class ChatGPTClient:
    """
    Заглушка-клиент: всегда возвращает одно и то же
    краткое сопроводительное письмо, не вызывая OpenAI.
    """

    def __init__(self):
        # Сохраняем ключ на будущее: когда будет бюджет,
        # достаточно раскомментировать код OpenAI-запроса ниже
        self.api_key = os.getenv("OPENAI_API_KEY")

    async def generate_cover_letter(self, job_description: str, resume_summary: str) -> str:
        """
        Возвращает готовый шаблон письма без обращения к внешним сервисам.
        """
        return (
            "Здравствуйте! Я внимательно изучил(а) вашу вакансию и вижу, "
            "что мой опыт и навыки хорошо подходят для решения описанных задач. "
            "Буду рад(а) обсудить детали сотрудничества и ответить на вопросы!"
        )

        # --- когда появится баланс, раскомментируйте этот блок ---
        # from openai import AsyncOpenAI
        # client = AsyncOpenAI(api_key=self.api_key)
        # response = await client.chat.completions.create(
        #     model="gpt-3.5-turbo",
        #     messages=[
        #         {"role": "system", "content": "Ты опытный HR, пиши на русском кратко и убедительно"},
        #         {"role": "user", "content": f"Вакансия: {job_description}"},
        #         {"role": "user", "content": f"Резюме: {resume_summary}"}
        #     ],
        #     temperature=0.7
        # )
        # return response.choices[0].message.content