# Phone Shop · Панель оператора

Рабочее место магазина техники: каталог и цены поставщиков, ручные витринные
цены, сравнение предложений и обращения покупателей через Telegram.

![Рабочая панель](docs/screenshots/dashboard.png)

## Посмотреть без запуска

Скриншоты работающего приложения сняты на отдельной базе с вымышленными данными.

### Каталог и ручные цены

![Каталог и ввод цен](docs/screenshots/catalog.png)

### Сравнение поставщиков

![Таблица сравнения](docs/screenshots/comparison.png)

### Подготовка прайса

![Конструктор прайса](docs/screenshots/telegram-price.png)

### Обращения покупателей

![Заявки Telegram](docs/screenshots/requests.png)

<details>
<summary>Авторизация и мобильная версия</summary>

![Авторизация](docs/screenshots/login.png)

<img src="docs/screenshots/mobile.png" width="390" alt="Мобильная рабочая панель">

</details>

## Статус релиза

Демонстрационная версия для портфолио. [Аудит кода](AUDIT.md) описывает
найденные проблемы и приоритеты перед промышленной эксплуатацией.
В релизе исправлены потеря ручных цен при агрегации, удаление старого прайса
до успешной отправки нового и доступ обычного оператора к диагностике.

## Модель данных

`Supplier → PriceList → Product` хранит исходные предложения.
`AggregatedProduct → ProductPrice` связывает варианты товара с поставщиками.
`OperatorPrice` задает ручную витринную цену с приоритетом при генерации.
`TelegramCustomerRequest → TelegramCustomerMessage` хранит историю диалога.

Демо-проект панели оператора для магазина техники на Django.

## Что умеет

- загрузка прайс-листов из файла или текста
- ручное обновление каталога и витринных цен
- таблица сравнения поставщиков
- генерация и публикация прайса в Telegram
- Telegram-бот для входящих клиентских обращений
- очередь заявок оператора с живым обновлением

## Стек

- Python
- Django
- PostgreSQL
- Telegram Bot API
- Bootstrap + кастомный CSS

## Локальный запуск

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Для просмотра без PostgreSQL задайте `DATABASE_URL=sqlite:///db.sqlite3` до
миграций. Команда `python manage.py seed_demo` добавляет вымышленные модели,
поставщиков и заявку в пустой каталог и не отправляет сообщений.
Общего пароля по умолчанию нет. Для UI-просмотра токен Telegram не требуется.

## Проверка и скриншоты

```bash
python manage.py test operators
python manage.py check
```

Для пересъемки установите `playwright` отдельно от runtime-зависимостей,
выполните `playwright install chromium`, запустите сайт на порту 8011 с отдельной
SQLite-базой и выполните `python scripts/capture_screenshots.py` с тем же
`DATABASE_URL`. Скрипт проверяет страницы, генерацию текста и мобильную ширину.

## Deploy

Проект подготовлен под бесплатный деплой на Render.

Смотрите:

- [DEPLOY_RENDER.md](./DEPLOY_RENDER.md)
- [render.yaml](./render.yaml)

## Важно

- На бесплатном Render файловая система эфемерная.
- Загруженные медиафайлы в таком режиме не являются постоянными.
- Telegram cleanup в демо-режиме работает лениво: при webhook-запросах и заходах в интерфейс.
- Bootstrap и шрифт загружаются с CDN: для полного оформления нужен интернет.
- Для публичного просмотра используйте вымышленные данные и отдельного тестового бота.
