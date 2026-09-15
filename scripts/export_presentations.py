#!/usr/bin/env python3
"""
Выгрузка описаний представлений из базы ЗУП обработкой SharedQueryDesignerHRM
(https://github.com/iljyxa/SharedQueryDesignerHRM) в data/<ВерсияЗУП>/ и
пересборка references/.

Что делает:
1. Запускает 1С в режиме ENTERPRISE с /Execute<обработка.epf> и
   /C"mode=batch_client;out=<временный каталог>". Обработка собирает описания
   по конфигурации базы, сохраняет по одному файлу <Подсистема>/<Имя>.json на
   представление и завершает сеанс.
2. Проверяет, что файлы появились, и целиком заменяет ими data/<ВерсияЗУП>/
   (представления, которых в новой выгрузке нет, из каталога версии исчезают).
3. Пересобирает references/ скриптом build_references.py (отключается --no-build).

Версия конфигурации в выгрузке не содержится, поэтому передаётся явно:
  --zup-version 3.1.38.92     версия ЗУП базы, из которой делается выгрузка
                              (Справка → О программе → Конфигурация)

Режим выгрузки (--mode):
  batch_client (по умолчанию)  форма обработки открывается, файлы пишет клиент 1С
                              на этот компьютер — работает и с серверной базой.
  batch                        форма не открывается, файлы пишет сервер 1С —
                              для файловой базы или когда сервер на этой машине.

Обработка (.epf):
  --processing <путь>   использовать указанный файл, ничего не скачивается.
  не указан             скачивается .epf последнего релиза со страницы GitHub
                        Releases репозитория SharedQueryDesignerHRM во
                        временный каталог; после работы он удаляется.

Подключение к базе 1С (обязательно):
  --infobase-path <путь>                              файловая база
  --infobase-server <сервер> --infobase-ref <ссылка>  серверная база
                                                      (один из двух вариантов)
  --username / --password                             если требуется
  --v8path                                            путь к 1cv8[.exe];
                                                      по умолчанию — автоопределение

Примеры:
  python scripts/export_presentations.py --zup-version 3.1.38.92 \\
      --infobase-path "C:\\bases\\zup" --username Администратор

  python scripts/export_presentations.py --zup-version 3.1.30.116 \\
      --infobase-server srv --infobase-ref zup_prod --username api --password "secret" \\
      --processing "C:\\tools\\КонструкторПредставленийЗарплатаКадры.epf"

Если при запуске 1С показывает предупреждение безопасности об открытии внешней
обработки и ждёт ответа — разрешите внешние обработки для этого запуска
(параметр DisableUnsafeActionProtection в conf.cfg) либо увеличьте --timeout и
подтвердите вручную.

Код возврата: 0 — успех, 1 — ошибка.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_references  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

GITHUB_REPO = "iljyxa/SharedQueryDesignerHRM"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

VERSION_RE = build_references.VERSION_RE


# --- Пути и вывод платформы (логика проверена на реальных 1cv8/ibcmd,
# см. https://github.com/Nikolay-Shirokov/cc-1c-skills) ---

def clean_path(value: str, param: str = "") -> str:
    """Прощает то, что однозначно лишнее в переданном пути: обрамляющие пробелы,
    обрамляющие кавычки, оставшиеся после разбора шеллом, конечный разделитель.
    Кавычка внутри пути после этого — явная ошибка, а не часть пути."""
    if not value:
        return value
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    if len(v) > 3 and v[-1] in "\\/":
        v = v[:-1]
    if '"' in v:
        print(f"Error: {param or 'путь'} содержит символ кавычки: {value}", file=sys.stderr)
        sys.exit(1)
    return v


def decode_platform_bytes(data: bytes) -> str:
    """1cv8 в batch-режиме обычно пишет через /Out в UTF-8, но при аварийном
    завершении может выдать в консоль текст в кодировке OEM. Пробуем UTF-8,
    иначе — cp866."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp866", errors="replace")


def redact(text: str, *secrets: str) -> str:
    """Вырезает из строки для вывода в лог литеральные значения секретов."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _version_dir(p: str) -> str:
    parent = os.path.dirname(p)
    if os.path.basename(parent).lower() == "bin":
        parent = os.path.dirname(parent)
    return os.path.basename(parent)


def _version_key(p: str):
    return [int(x) for x in re.findall(r"\d+", _version_dir(p))]


def resolve_v8path(v8path: str) -> str:
    """Определяет путь к исполняемому файлу 1cv8."""
    if not v8path:
        if os.name == "nt":
            candidates = (
                glob.glob(r"C:\Program Files\1cv8\*\bin\1cv8.exe")
                + glob.glob(r"C:\Program Files (x86)\1cv8\*\bin\1cv8.exe")
            )
        else:
            candidates = glob.glob("/opt/1cv8/*/1cv8")
        if candidates:
            v8path = max(candidates, key=_version_key)
            print(f"Автоопределена платформа {_version_dir(v8path)}: {v8path}")
        else:
            print("Error: исполняемый файл 1С не найден. Укажите --v8path", file=sys.stderr)
            sys.exit(1)
    if os.path.isdir(v8path):
        exe = "1cv8.exe" if os.name == "nt" else "1cv8"
        v8path = os.path.join(v8path, exe)
    if not os.path.isfile(v8path):
        print(f"Error: исполняемый файл 1С не найден: {v8path}", file=sys.stderr)
        sys.exit(1)
    return v8path


def assert_infobase_exists(path: str) -> None:
    if not path:
        return
    if not os.path.isfile(os.path.join(path, "1Cv8.1CD")):
        print(f"Error: информационная база не найдена по пути {path} (нет 1Cv8.1CD)", file=sys.stderr)
        sys.exit(1)


def run_v8(v8path: str, arguments: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Запускает 1cv8 и ждёт завершения, захватывая вывод.

    Аргументы несут собственные кавычки внутри значения (/F"путь", /N"user") —
    так их ожидает разбирать сама 1С, и на Windows, и на *nix. На Windows
    список аргументов склеивается в одну командную строку как есть.
    На POSIX список уходит subprocess-у как есть (без шелла), поэтому
    обрамляющие кавычки, нужные только для склейки на Windows, снимаются —
    иначе 1cv8 получит их как часть значения буквально."""
    if os.name == "nt":
        cmd = '"' + v8path + '" ' + " ".join(arguments)
    else:
        def strip_framing_quotes(a: str) -> str:
            if len(a) > 1 and a[0] == '"' and a[-1] == '"':
                return a[1:-1]
            if a[0:1] == "/" and a[-1:] == '"' and '"' in a[:-1]:
                i = a.index('"')
                return a[:i] + a[i + 1:-1]
            return a
        cmd = [v8path] + [strip_framing_quotes(a) for a in arguments]
    try:
        r = subprocess.run(cmd, input=b"", capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        print(
            f"Error: 1С не завершилась за {timeout} с — процесс остановлен. Обычно это значит, что "
            "сеанс ждёт ответа в диалоге (предупреждение безопасности, выбор пользователя, "
            "сообщение об ошибке). Проверьте лог ниже и параметры подключения.",
            file=sys.stderr,
        )
        r = subprocess.CompletedProcess(cmd, returncode=-1, stdout=e.stdout or b"", stderr=e.stderr or b"")
    r.stdout = decode_platform_bytes(r.stdout)
    r.stderr = decode_platform_bytes(r.stderr)
    return r


def print_platform_output(result: subprocess.CompletedProcess) -> None:
    text = ((result.stdout or "") + (result.stderr or "")).rstrip()
    if not text:
        return
    limit = 65536
    if len(text) > limit:
        text = f"[... обрезано, показаны последние {limit} символов ...]\n" + text[-limit:]
    print("--- Вывод платформы ---")
    print(text)
    print("--- End ---")


# --- Получение обработки ---

def download_latest_processing(dest_dir: Path) -> Path:
    """Скачивает .epf последнего релиза SharedQueryDesignerHRM в dest_dir."""
    print(f"Обработка не указана — определяю последний релиз {GITHUB_REPO}...")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "SharedQuerySchemesHRM-skill-export",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(GITHUB_LATEST_RELEASE_API, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"Error: не удалось получить информацию о релизе {GITHUB_REPO}: {e}", file=sys.stderr)
        sys.exit(1)

    assets = release.get("assets", [])
    epf_asset = next((a for a in assets if a.get("name", "").lower().endswith(".epf")), None)
    if epf_asset is None:
        print(
            f"Error: в релизе {release.get('tag_name', '?')} репозитория {GITHUB_REPO} "
            "не найден файл .epf",
            file=sys.stderr,
        )
        sys.exit(1)

    url = epf_asset["browser_download_url"]
    dest = dest_dir / epf_asset["name"]
    print(f"Скачиваю {epf_asset['name']} ({release.get('tag_name', '?')}) из {url} ...")
    try:
        urllib.request.urlretrieve(url, dest)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"Error: не удалось скачать обработку: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Обработка сохранена во временный файл: {dest}")
    return dest


# --- Выгрузка ---

def run_export(args, v8path: str, processing_path: str, out_dir: Path) -> bool:
    """Запускает обработку в пакетном режиме. Возвращает True, если 1С завершилась
    успешно и в out_dir появились файлы представлений."""
    log_dir = tempfile.mkdtemp(prefix="1c_execute_log_")
    try:
        log_file = os.path.join(log_dir, "execute_log.txt")

        arguments = ["ENTERPRISE"]
        if args.infobase_server and args.infobase_ref:
            arguments.extend(["/S", f'"{args.infobase_server}/{args.infobase_ref}"'])
        else:
            arguments.extend(["/F", f'"{args.infobase_path}"'])
        if args.username:
            arguments.append(f'/N"{args.username}"')
        if args.password:
            arguments.append(f'/P"{args.password}"')
        arguments.append(f'/Execute"{processing_path}"')
        arguments.append(f'/C"mode={args.mode};out={out_dir}"')
        arguments.extend(["/Out", f'"{log_file}"'])
        arguments.append("/DisableStartupDialogs")
        arguments.append("/DisableStartupMessages")

        display_cmd = redact(" ".join(arguments), args.password, args.username)
        print(f"Running: {os.path.basename(v8path)} {display_cmd}")

        result = run_v8(v8path, arguments, args.timeout)

        if os.path.isfile(log_file):
            try:
                with open(log_file, "r", encoding="utf-8-sig") as f:
                    log_content = f.read()
                if log_content:
                    print("--- Log ---")
                    print(log_content)
                    print("--- End ---")
            except OSError:
                pass

        print_platform_output(result)

        if result.returncode != 0:
            print(f"Error: 1С завершилась с кодом {result.returncode}", file=sys.stderr)
            return False

        if next(out_dir.rglob("*.json"), None) is None:
            hint = (
                "файлы пишет сервер 1С — для серверной базы они остаются на сервере, используйте "
                "--mode batch_client" if args.mode == "batch" else
                "проверьте вывод платформы выше: возможно, сеанс завершился до сохранения файлов"
            )
            print(
                f"Error: после выполнения в {out_dir} не найдено ни одного .json — {hint}",
                file=sys.stderr,
            )
            return False
        return True
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)


def replace_version_data(out_dir: Path, version: str) -> Path:
    """Целиком заменяет data/<version>/ содержимым выгрузки."""
    target = DATA_DIR / version
    if target.exists():
        shutil.rmtree(target)
    DATA_DIR.mkdir(exist_ok=True)
    shutil.copytree(out_dir, target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Выгрузка описаний представлений ЗУП обработкой SharedQueryDesignerHRM в data/<ВерсияЗУП>/",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--zup-version", required=True,
        help="Версия конфигурации ЗУП базы, например 3.1.38.92; задаёт каталог data/<версия>/",
    )
    parser.add_argument(
        "--mode", choices=("batch_client", "batch"), default="batch_client",
        help="Режим обработки: batch_client — файлы пишет клиент на этот компьютер (по умолчанию); "
             "batch — файлы пишет сервер 1С",
    )
    parser.add_argument(
        "--processing", default="",
        help="Путь к .epf обработки SharedQueryDesignerHRM; если не задан — "
             "скачивается последний релиз с GitHub во временный каталог",
    )
    parser.add_argument("--v8path", default="", help="Путь к 1cv8[.exe]; по умолчанию автоопределение")
    parser.add_argument("--infobase-path", default="", help="Путь к файловой информационной базе")
    parser.add_argument("--infobase-server", default="", help="Имя сервера 1С (для серверной базы)")
    parser.add_argument("--infobase-ref", default="", help="Имя базы на сервере (для серверной базы)")
    parser.add_argument("--username", default="", help="Имя пользователя 1С")
    parser.add_argument("--password", default="", help="Пароль пользователя 1С")
    parser.add_argument(
        "--timeout", type=int, default=1800,
        help="Сколько секунд ждать завершения 1С (по умолчанию 1800)",
    )
    parser.add_argument("--no-build", action="store_true", help="Не пересобирать references/ после выгрузки")
    args = parser.parse_args()

    if not VERSION_RE.match(args.zup_version):
        print(f"Error: --zup-version ожидает версию вида 3.1.38.92, получено: {args.zup_version}", file=sys.stderr)
        return 1

    args.infobase_path = clean_path(args.infobase_path, "--infobase-path")
    args.processing = clean_path(args.processing, "--processing")

    if not args.infobase_path and not (args.infobase_server and args.infobase_ref):
        print(
            "Error: укажите --infobase-path (файловая база) либо "
            "--infobase-server и --infobase-ref (серверная база)",
            file=sys.stderr,
        )
        return 1

    assert_infobase_exists(args.infobase_path)
    if args.infobase_path:
        args.infobase_path = os.path.abspath(args.infobase_path)
    v8path = resolve_v8path(args.v8path)

    temp_processing_dir = None
    processing_path = args.processing
    out_dir = Path(tempfile.mkdtemp(prefix="zup_presentations_"))
    try:
        if not processing_path:
            temp_processing_dir = tempfile.mkdtemp(prefix="shared_query_designer_")
            processing_path = str(download_latest_processing(Path(temp_processing_dir)))
        elif not os.path.isfile(processing_path):
            print(f"Error: файл обработки не найден: {processing_path}", file=sys.stderr)
            return 1
        else:
            processing_path = os.path.abspath(processing_path)

        if not run_export(args, v8path, processing_path, out_dir):
            return 1

        target = replace_version_data(out_dir, args.zup_version)
        count = sum(1 for _ in target.rglob("*.json"))
        print(f"OK: {count} представлений → {target.relative_to(ROOT)}/")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        if temp_processing_dir:
            shutil.rmtree(temp_processing_dir, ignore_errors=True)

    if args.no_build:
        print("Сборка references/ пропущена (--no-build); запустите: python scripts/build_references.py")
        return 0
    return build_references.main()


if __name__ == "__main__":
    sys.exit(main())
