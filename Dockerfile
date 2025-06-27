FROM python:3.9-slim

WORKDIR /app

# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода приложения
COPY . .

# Hugging Face использует порт 7860 по умолчанию
ENV PORT=7860
EXPOSE $PORT

# Запуск приложения
CMD ["gunicorn", "--bind", "0.0.0.0:$PORT", "flask_app:app"]
