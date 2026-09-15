#!/usr/bin/env python3
"""
Генерация справочников references/ из выгрузок обработки SharedQueryDesignerHRM.

Вход:  data/<ВерсияЗУП>/<Подсистема>/<Имя>.json — файлы, которые обработка
       пишет в пакетном режиме (mode=batch_client / mode=batch), как есть.
Выход: references/<ВерсияЗУП>/<Подсистема>/<Имя>.md — по одному файлу на представление,
       references/index.md                            — список версий и представлений.

Статичные документы в references/ (механизм-представлений.md,
программный-интерфейс.md) скрипт не трогает; каталоги версий и index.md
пересоздаются целиком, каталоги версий, которых больше нет в data/, удаляются.

Проверки перед генерацией (любая ошибка — код возврата 1):
- имя каталога в data/ — версия конфигурации вида 3.1.38.92;
- файл — валидный JSON со всеми ключами формата выгрузки;
- имя файла совпадает с полем "Имя", каталог — с полем "Подсистема";
- имена представлений внутри версии не повторяются.

Запуск:
  python scripts/build_references.py
"""

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REFERENCES_DIR = ROOT / "references"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")

# Ключи файла выгрузки — см. README обработки, раздел «Пакетный режим».
REQUIRED_KEYS = (
    "Подсистема", "Имя",
    "ЕстьИсточникДанных", "ЕстьФильтр", "Фильтр", "ФильтрОбязателен", "ДоступныОтборы",
    "Основное", "Описание", "ЕстьПрограммныйИнтерфейс", "Шаблон",
    "ДоступныеПараметры", "ДоступныеПоля",
)

DEFAULT_FILTER_TABLE = "ВТФильтр"
SKELETON_FIELDS_LIMIT = 5


# --- Загрузка и проверка ---

def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def subsystem_dir_name(subsystem: str) -> str:
    """Имя подкаталога выгрузки — как его формирует обработка."""
    return subsystem.replace(" ", "_") if subsystem else "Общие"


def load_data() -> tuple[dict[str, list[dict]], list[str]]:
    """Возвращает {версия: [представления]} и список ошибок."""
    versions: dict[str, list[dict]] = {}
    errors: list[str] = []

    if not DATA_DIR.is_dir():
        return versions, errors

    for version_dir in sorted(DATA_DIR.iterdir()):
        if version_dir.name.startswith("."):
            continue
        if not version_dir.is_dir() or not VERSION_RE.match(version_dir.name):
            errors.append(
                f"{version_dir.relative_to(ROOT)}: в data/ ожидаются только каталоги версий "
                "конфигурации вида 3.1.38.92"
            )
            continue

        entries: list[dict] = []
        seen: dict[str, Path] = {}
        for path in sorted(version_dir.rglob("*.json")):
            rel = path.relative_to(ROOT)
            try:
                with path.open(encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                errors.append(f"{rel}: невалидный JSON — {e}")
                continue
            if not isinstance(data, dict):
                errors.append(f"{rel}: ожидается объект JSON")
                continue

            missing = [k for k in REQUIRED_KEYS if k not in data]
            if missing:
                errors.append(
                    f"{rel}: нет ключей {', '.join(missing)} — файл выгружен не той версией "
                    "обработки или это не файл представления"
                )
                continue

            name = data["Имя"]
            if path.name != f"{name}.json":
                errors.append(f"{rel}: имя файла не совпадает с полем «Имя» = {name}")
            expected_dir = subsystem_dir_name(data["Подсистема"])
            if path.parent.name != expected_dir:
                errors.append(f"{rel}: каталог не совпадает с полем «Подсистема» (ожидался {expected_dir}/)")
            if name in seen:
                errors.append(f"{rel}: представление {name} уже есть в {seen[name].relative_to(ROOT)}")
            seen[name] = path
            entries.append(data)

        if not entries:
            errors.append(f"{version_dir.relative_to(ROOT)}: нет ни одного файла представления")
        versions[version_dir.name] = entries

    return versions, errors


# --- Рендеринг ---

def md_cell(text) -> str:
    """Текст для ячейки таблицы Markdown: без переводов строк и незакрытых «|»."""
    if text is None:
        return ""
    return " ".join(str(text).split()).replace("|", "\\|")


def query_literal(value, is_expression: bool) -> str | None:
    """Значение параметра в записи языка запросов — как его формирует обработка
    (ЗначениеПараметраВВыражениеЯзыкаЗапроса); выражения подставляются как есть."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "ИСТИНА" if value else "ЛОЖЬ"
    if isinstance(value, (int, float)):
        return str(value)
    if is_expression:
        return str(value)
    return f'"{value}"'


def render_default(param: dict) -> str:
    literal = query_literal(param.get("Значение"), bool(param.get("ЗначениеЭтоВыражение")))
    if literal is None:
        return "—"
    result = f"`{literal}`"
    if param.get("ЗначениеЭтоВыражение") and isinstance(param.get("Значение"), str):
        result += " (выражение)"
    allowed = [v.get("Значение") for v in param.get("ДоступныеЗначения", []) if v.get("Значение") is not None]
    if allowed:
        result += "; допустимые: " + ", ".join(f"`{v}`" for v in allowed)
    return result


def render_flags(entry: dict) -> list[str]:
    lines = [f"- **Основное**: {'да' if entry['Основное'] else 'нет'}"]

    if entry["ЕстьФильтр"]:
        name = entry["Фильтр"] or ""
        text = "обязательна" if entry["ФильтрОбязателен"] else "не обязательна"
        if name:
            text += f", типовой механизм ожидает таблицу с именем `{name}`"
        lines.append(f"- **Таблица фильтра**: {text}")
    else:
        lines.append("- **Таблица фильтра**: не используется")

    if entry["ЕстьИсточникДанных"]:
        lines.append(
            "- **Источник данных**: требуется регистр сведений — его имя добавляется к имени "
            f"временной таблицы: `Представления_{entry['Имя']}_<ИмяРегистра>`; состав полей "
            "определяется измерениями, ресурсами и реквизитами регистра"
        )

    lines.append(f"- **Отборы по полям**: {'поддерживаются' if entry['ДоступныОтборы'] else 'нет'}")
    lines.append(f"- **Программный интерфейс**: {'есть' if entry['ЕстьПрограммныйИнтерфейс'] else 'нет'}")
    return lines


def render_params(params: list[dict]) -> list[str]:
    if not params:
        return ["Параметров нет.", ""]
    lines = [
        "| Имя | Тип | Обязателен | По умолчанию | Описание |",
        "|---|---|---|---|---|",
    ]
    for p in params:
        types = ", ".join(p.get("Тип") or []) or "—"
        required = "Да" if p.get("Обязательный") else "Нет"
        lines.append(
            f"| {md_cell(p['Имя'])} | {md_cell(types)} | {required} | "
            f"{md_cell(render_default(p))} | {md_cell(p.get('Описание'))} |"
        )
    lines.append("")
    return lines


def render_fields(entry: dict) -> list[str]:
    fields = entry["ДоступныеПоля"]
    if not fields:
        if entry["ЕстьИсточникДанных"]:
            return [
                "Список полей не фиксирован: это измерения, ресурсы и реквизиты выбранного регистра "
                "(плюс `Период`, `ДатаНачала`/`ДатаОкончания` в зависимости от представления). "
                "Выражение пустого значения подбирается по типу поля регистра, см. «Поля» в "
                "`references/механизм-представлений.md`.",
                "",
            ]
        return ["Полей нет (представление недоступно в базе, из которой сделана выгрузка).", ""]
    lines = [
        "| Имя | Выражение | Описание |",
        "|---|---|---|",
    ]
    for f in fields:
        lines.append(f"| {md_cell(f['Имя'])} | `{md_cell(f.get('Выражение'))}` | {md_cell(f.get('Описание'))} |")
    lines.append("")
    return lines


def render_skeleton(entry: dict) -> list[str]:
    """Минимальный корректный текст представления: несколько полей, обязательный
    фильтр и обязательные параметры со значениями по умолчанию."""
    name = entry["Имя"]
    fields = entry["ДоступныеПоля"]

    select_lines: list[str] = []
    if fields:
        for f in fields[:SKELETON_FIELDS_LIMIT]:
            select_lines.append(f"\t{f['Выражение']} КАК {f['Имя']}")
        if len(fields) > SKELETON_FIELDS_LIMIT:
            select_lines[-1] += f"  // … и другие поля из таблицы выше (всего {len(fields)})"
    elif entry["ЕстьИсточникДанных"]:
        select_lines.append("\t<ВыражениеПустогоЗначения> КАК <ИмяПоляРегистра>  // измерения, ресурсы, реквизиты регистра")
    else:
        select_lines.append("\tНЕОПРЕДЕЛЕНО КАК <ИмяПоля>")

    table_name = f"Представления_{name}"
    if entry["ЕстьИсточникДанных"]:
        table_name += "_<ИмяРегистра>"

    # Запятая после каждого поля, кроме последнего; комментарий остаётся в конце строки.
    lines = ["ВЫБРАТЬ"]
    for i, sel in enumerate(select_lines):
        if i < len(select_lines) - 1:
            head, sep, comment = sel.partition("  //")
            lines.append(f"{head},{sep}{comment}")
        else:
            lines.append(sel)
    lines.append(f"ПОМЕСТИТЬ {table_name}")

    if entry["ЕстьФильтр"] and entry["ФильтрОбязателен"]:
        filter_name = entry["Фильтр"] or DEFAULT_FILTER_TABLE
        lines.append("ИЗ")
        lines.append(f"\t{filter_name} КАК {filter_name}")

    conditions = []
    for p in entry["ДоступныеПараметры"]:
        if not p.get("Обязательный"):
            continue
        literal = query_literal(p.get("Значение"), bool(p.get("ЗначениеЭтоВыражение")))
        if literal is None:
            literal = f"<{p['Имя']}>"
        conditions.append(f'"{p["Имя"]}" = {literal}')
    if conditions:
        lines.append("ГДЕ")
        lines.append("\t" + "\n\tИ ".join(conditions))

    return ["```bsl", *lines, "```", ""]


def render_sharedquery_file(version: str, entry: dict) -> str:
    lines = [
        f"# {entry['Имя']} (ЗУП {version}, подсистема {entry['Подсистема'] or 'Общие'})", "",
        "Сгенерировано из `data/` скриптом `scripts/build_references.py`, вручную не редактируется. "
        "Синтаксис и правила применения — `references/механизм-представлений.md`.", "",
    ]
    lines += render_flags(entry)
    lines.append("")
    if entry["Описание"]:
        lines += [entry["Описание"].strip(), ""]

    lines += ["## Параметры", ""]
    lines += render_params(entry["ДоступныеПараметры"])

    lines += ["## Поля", ""]
    lines += render_fields(entry)

    lines += ["## Заготовка текста представления", ""]
    lines += render_skeleton(entry)

    if entry["ЕстьПрограммныйИнтерфейс"] and entry["Шаблон"]:
        lines += [
            "## Программный интерфейс", "",
            "Шаблон кода на встроенном языке; правила подстановки плейсхолдеров `<…>` — "
            "в `references/программный-интерфейс.md`.", "",
            "```bsl",
            entry["Шаблон"].replace("\r\n", "\n").rstrip(),
            "```", "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def yes_no(flag: bool) -> str:
    return "да" if flag else "—"


def sharedquery_rel_path(version: str, entry: dict) -> str:
    """Путь к файлу представления относительно корня репозитория."""
    return f"references/{version}/{subsystem_dir_name(entry['Подсистема'])}/{entry['Имя']}.md"


def render_index(versions: dict[str, list[dict]]) -> str:
    lines = [
        "# Указатель представлений", "",
        "Сгенерировано скриптом `scripts/build_references.py`, вручную не редактируется.", "",
        "Каждый каталог `references/<ВерсияЗУП>/` — выгрузка из базы с этой версией конфигурации. "
        "Для пользователя с версией ЗУП X бери каталог с наибольшей версией, не превышающей X; "
        "если версия пользователя неизвестна — самый новый каталог, и скажи об этом допущении.", "",
    ]
    if not versions:
        lines += ["Выгрузок пока нет — см. `README.md`, раздел «Выгрузка из базы ЗУП».", ""]
        return "\n".join(lines)

    ordered = sorted(versions, key=version_key, reverse=True)
    lines += ["Версии (новые сверху): " + ", ".join(f"`{v}`" for v in ordered) + ".", ""]

    for version in ordered:
        entries = sorted(versions[version], key=lambda e: (not e["Основное"], e["Подсистема"], e["Имя"]))
        main_count = sum(1 for e in entries if e["Основное"])
        lines += [
            f"## ЗУП {version}", "",
            f"Каталог `references/{version}/`, представлений: {len(entries)} (основных: {main_count}). "
            "Основные — те, что нужны в повседневных задачах; остальные зависят от подсистем и "
            "функциональных опций базы.", "",
            "| Представление | Подсистема | Основное | Фильтр | Источник | Отборы | ПИ | Файл |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for e in entries:
            filter_text = "—"
            if e["ЕстьФильтр"]:
                filter_text = "обязателен" if e["ФильтрОбязателен"] else "необязателен"
            lines.append(
                f"| {e['Имя']} | {md_cell(e['Подсистема'])} | {yes_no(e['Основное'])} | {filter_text} | "
                f"{yes_no(e['ЕстьИсточникДанных'])} | {yes_no(e['ДоступныОтборы'])} | "
                f"{yes_no(e['ЕстьПрограммныйИнтерфейс'])} | `{sharedquery_rel_path(version, e)}` |"
            )
        lines.append("")

    return "\n".join(lines)


# --- Запись ---

def clean_generated(keep_versions: set[str]) -> None:
    """Удаляет каталоги версий, которых больше нет в data/, и старые сгенерированные файлы."""
    if not REFERENCES_DIR.is_dir():
        return
    for item in REFERENCES_DIR.iterdir():
        if item.is_dir() and VERSION_RE.match(item.name):
            shutil.rmtree(item)
        elif item.name == "index.md":
            item.unlink()


def write_references(versions: dict[str, list[dict]]) -> int:
    REFERENCES_DIR.mkdir(exist_ok=True)
    clean_generated(set(versions))

    files_written = 0
    for version, entries in versions.items():
        for entry in entries:
            path = REFERENCES_DIR / sharedquery_rel_path(version, entry).removeprefix("references/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_sharedquery_file(version, entry), encoding="utf-8", newline="\n")
            files_written += 1

    (REFERENCES_DIR / "index.md").write_text(render_index(versions), encoding="utf-8", newline="\n")
    return files_written


def main() -> int:
    versions, errors = load_data()
    if errors:
        print(f"Найдено ошибок: {len(errors)}", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    files_written = write_references(versions)
    if not versions:
        print("Предупреждение: в data/ нет выгрузок — сгенерирован только пустой references/index.md.")
        return 0

    total = sum(len(v) for v in versions.values())
    print(f"OK: версий {len(versions)}, представлений {total}, файлов {files_written} → {REFERENCES_DIR.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
