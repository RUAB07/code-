FROM python:3.12-slim

WORKDIR /app

COPY . /app

EXPOSE 8000

CMD ["python", "-m", "http.server", "8000", "--bind", "0.0.0.0"]
