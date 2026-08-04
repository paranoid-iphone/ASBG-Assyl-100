# Локальный запуск через Docker и ngrok

Docker Compose запускает три связанных контейнера:

- `app` — сайт, админка и Telegram-бот;
- `db` — PostgreSQL с постоянным хранилищем;
- `ngrok` — публичный HTTPS-туннель к приложению.

## Первый запуск

Скопируйте пример настроек:

```powershell
Copy-Item .env.example .env
```

В `.env` обязательно заполните:

```text
TELEGRAM_BOT_TOKEN=токен BotFather
NGROK_AUTHTOKEN=токен из кабинета ngrok
ADMIN_PASSWORD=сложный пароль админки
SECRET_KEY=длинная случайная строка
POSTGRES_PASSWORD=сложный пароль латинскими буквами и цифрами
```

Запустите весь комплект:

```powershell
docker compose up --build -d
```

Проверьте состояние:

```powershell
docker compose ps
docker compose logs -f app ngrok
```

Админка доступна локально:

```text
http://127.0.0.1:8000/admin
```

На странице «Обзор» приложение автоматически покажет публичную ссылку ngrok для большого экрана. Локальная диагностическая страница ngrok доступна по адресу `http://127.0.0.1:4040`.

## Остановка

```powershell
docker compose down
```

Обычная остановка сохраняет PostgreSQL в Docker volume `game_postgres_data`. Не используйте `docker compose down -v`, если не хотите безвозвратно удалить базу мероприятия.

## Обновление после изменений

```powershell
git pull
docker compose up --build -d
```

## Резервная копия базы

```powershell
docker compose exec db pg_dump -U game_admin -d intellectual_game -Fc -f /tmp/game.backup
docker compose cp db:/tmp/game.backup ./game.backup
```
