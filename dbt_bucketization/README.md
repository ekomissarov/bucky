Welcome to your new dbt project!

### Using the starter project

Try running the following commands:
- dbt run
- dbt test


### Resources:
- Learn more about dbt [in the docs](https://docs.getdbt.com/docs/introduction)
- Check out [Discourse](https://discourse.getdbt.com/) for commonly asked questions and answers
- Join the [chat](https://community.getdbt.com/) on Slack for live discussions and support
- Find [dbt events](https://events.getdbt.com) near you
- Check out [the blog](https://blog.getdbt.com/) for the latest news on dbt's development and best practices


# Структура dbt-проекта

Каждая директория выполняет свою четкую роль в процессе трансформации данных:

## Основные директории

* **`models/`** — **Самая главная папка.** Здесь хранятся `.sql` файлы с вашей бизнес-логикой и витринами (трансформациями). Каждый `.sql` файл внутри этой директории превращается в отдельную таблицу или представление (View) в DuckDB при выполнении команды `dbt run`.
* **`macros/`** — Папка для reusable-кода и функций на Jinja[cite: 1]. Если какой-то SQL-кусок или расчет повторяется из модели в модель, вы выносите его в макрос и вызываете как функцию в своих SQL-файлах.
* **`seeds/`** — Для небольших статических `.csv` файлов[cite: 1] (например, справочники категорий, словарь сопоставления ID, списки стран). При запуске команды `dbt seed` dbt автоматически загружает данные из этих CSV-файлов прямо в таблицы DuckDB.
* **`tests/`** — Кастомные SQL-тесты на качество данных[cite: 1]. Вы пишете SQL-запрос, который возвращает "плохие" строки (например, дубликаты или `NULL` значения). Если запрос ничего не вернул — тест пройден (`dbt test`).
* **`snapshots/`** — Механизм для поддержки **SCD Type 2** (Slowly Changing Dimensions)[cite: 1]. Используется для отслеживания истории изменений строк во времени (например, если у юзера меняется статус или атрибуты, dbt сам проставит метки времени `valid_from` и `valid_to`).
* **`analyses/`** — Для разовых аналитических SQL-запросов[cite: 1]. Файлы отсюда компилируются dbt (в них можно использовать Jinja и `ref()`), но **не создаются** как таблицы или View в DuckDB при вызове `dbt run`.
* **`logs/`** — Автоматически создаваемая директория, где хранятся детальные лог-файлы работы dbt (`dbt.log`)[cite: 1]. Обычно эту папку добавляют в `.gitignore`.

---

## Конфигурационные файлы

* **`dbt_project.yml`** — Главный конфигурационный файл проекта[cite: 1]. В нем задается имя проекта, ссылка на `profile` из `profiles.yml`, пути к папкам, а также глобальные настройки для моделей (например, материализация по умолчанию: `view` или `table`).
* **`README.md`** — Документация к вашему репозиторию[cite: 1].