"""Build the editable Russian journey SVG and its matching PNG preview.

Run: python scripts/build_diagram.py [--out docs/diagrams]
SVG text remains editable. All visible text uses exactly 24 or 18 px.
Pillow is used only for font measurements and the PNG preview; no network or
image-generation service is involved. Install Pillow to rebuild the preview.
The source contains the wording, positions, connectors and two-size typography.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html import escape
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT = 2610, 1640
TITLE_SIZE, BODY_SIZE = 24, 18
BACKGROUND = "#F5EFEB"
INK = "#123638"
GREEN = "#1C7134"
TEAL = "#073F42"
LINE = "#688381"
MUTED = "#697675"
GRAY = "#E3E4E1"
FONT_FAMILY = "Segoe UI, Arial, sans-serif"
EXECUTION_COLORS = {"code": "#1C7134", "model": "#2C7E8D", "tools": "#B47829", "unavailable": "#8A9290"}


def fonts():
    candidates = [
        (Path("C:/Windows/Fonts/segoeui.ttf"), Path("C:/Windows/Fonts/segoeuib.ttf")),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
         Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
    ]
    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            return ImageFont.truetype(str(regular), BODY_SIZE), ImageFont.truetype(str(bold), TITLE_SIZE)
    raise RuntimeError("Install Segoe UI or DejaVu Sans to render the two-size diagram")


BODY_FONT, TITLE_FONT = fonts()


def measure(text, size=BODY_SIZE):
    return (TITLE_FONT if size == TITLE_SIZE else BODY_FONT).getlength(text)


def wrap(text, width, size):
    lines = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            proposed = (current + " " + word).strip()
            if current and measure(proposed, size) > width:
                lines.append(current)
                current = word
            else:
                current = proposed
        lines.append(current)
    return lines


@dataclass
class Node:
    key: str
    x: float
    y: float
    w: float
    h: float
    title: str
    body: str = ""
    shape: str = "box"
    style: str = "normal"
    inclusive: bool = False
    execution: str = "model"

    @property
    def left(self):
        return self.x, self.y + self.h / 2

    @property
    def right(self):
        return self.x + self.w, self.y + self.h / 2

    @property
    def top(self):
        return self.x + self.w / 2, self.y

    @property
    def bottom(self):
        return self.x + self.w / 2, self.y + self.h


class Canvas:
    def __init__(self, height=HEIGHT, width=WIDTH):
        self.height = height
        self.width = width
        self.image = Image.new("RGB", (width, height), BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)
        self.primitives = []
        self.group = None
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="diagram-title diagram-desc">',
                      '<title id="diagram-title">Путь переноса памяти и настройки другого агента</title>',
                      '<desc id="diagram-desc">Версия 0.8.4. Один ведущий объединяет источники, сохраняет выбранные данные и проверяет общий профиль. Для ГигаАгента пользователь получает полный ZIP и отдельно два Markdown-файла; в ГигаАгент загружаются только два Markdown-файла. Экспортёр выдаёт файлы в чате. Пакетный импорт сохраняет отчёты, сверяет текущую память и проверяет настройку в выбранном объёме. Неизвестная память блокирует активацию, но не сохранение файлов.</desc>',
                      '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L8,4 L0,8 Z" fill="' + LINE + '"/></marker></defs>',
                      f'<rect width="{width}" height="{height}" fill="{BACKGROUND}"/>']

    def rect(self, x, y, w, h, *, fill, stroke=None, radius=16, width=1.5):
        self.primitives.append({"type": "rect", "x": x, "y": y, "w": w, "h": h,
                                "fill": fill, "stroke": stroke, "radius": radius, "width": width, "group": self.group})
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}"' +
                          (f' stroke="{stroke}" stroke-width="{width}"' if stroke else '') + '/>')
        self.draw.rounded_rectangle((x, y, x + w, y + h), radius, fill=fill, outline=stroke, width=round(width))

    def circle(self, x, y, radius, *, fill=BACKGROUND, stroke=LINE, width=2):
        self.primitives.append({"type": "circle", "x": x, "y": y, "radius": radius,
                                "fill": fill, "stroke": stroke, "width": width, "group": self.group})
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>')
        self.draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=fill, outline=stroke, width=width)

    def text(self, x, y, text, *, size=BODY_SIZE, color=INK, anchor="start", backing=False):
        assert size in {TITLE_SIZE, BODY_SIZE}
        font = TITLE_FONT if size == TITLE_SIZE else BODY_FONT
        width = measure(text, size)
        left = x - width / 2 if anchor == "middle" else x
        if backing:
            self.rect(left - 4, y - 2, width + 8, size + 8, fill=BACKGROUND, radius=3)
        self.primitives.append({"type": "text", "x": left, "y": y, "w": width + 10,
                                "text": text, "size": size, "color": color, "group": self.group})
        weight = "700" if size == TITLE_SIZE else "400"
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{FONT_FAMILY}" font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}" dominant-baseline="text-before-edge">{escape(text)}</text>')
        # Use the ascender anchor, compensating for the font's internal top gap.
        box = font.getbbox(text or " ")
        self.draw.text((left, y - box[1]), text, font=font, fill=color)

    def path(self, points, *, arrow=True, dashed=False, width=2.4):
        self.primitives.append({"type": "path", "points": points, "arrow": arrow,
                                "dashed": dashed, "width": width, "color": LINE, "group": self.group})
        pts = " ".join(f"{x},{y}" for x, y in points)
        self.parts.append(f'<polyline points="{pts}" fill="none" stroke="{LINE}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"' +
                          (' stroke-dasharray="7 6"' if dashed else '') + (' marker-end="url(#arrow)"' if arrow else '') + '/>')
        if dashed:
            for start, end in zip(points, points[1:]):
                distance = math.dist(start, end)
                for offset in range(0, math.ceil(distance), 13):
                    a, b = offset/distance, min(offset+7, distance)/distance
                    self.draw.line((start[0]+(end[0]-start[0])*a, start[1]+(end[1]-start[1])*a,
                                    start[0]+(end[0]-start[0])*b, start[1]+(end[1]-start[1])*b), fill=LINE, width=2)
        else:
            self.draw.line(points, fill=LINE, width=round(width), joint="curve")
        if arrow:
            x, y = points[-1]
            px, py = points[-2]
            length = math.hypot(x-px, y-py)
            ux, uy = (x-px)/length, (y-py)/length
            self.draw.polygon([(x, y), (x-8*ux+4*uy, y-8*uy-4*ux), (x-8*ux-4*uy, y-8*uy+4*ux)], fill=LINE)

    def node(self, node):
        self.group = node.key
        self.parts.append(f'<g id="{node.key}"><title>{escape(node.title)}</title>')
        edge = EXECUTION_COLORS[node.execution]
        if node.shape == "diamond":
            cx, cy = node.top[0], node.left[1]
            points = [node.top, node.right, node.bottom, node.left]
            self.primitives.append({"type": "diamond", "x": node.x, "y": node.y, "w": node.w, "h": node.h,
                                    "fill": "#EBF3E9", "stroke": edge, "width": 2, "group": self.group})
            self.parts.append('<polygon points="' + ' '.join(f'{x},{y}' for x,y in points) + f'" fill="#EBF3E9" stroke="{edge}" stroke-width="2"/>')
            self.draw.polygon(points, fill="#EBF3E9", outline=edge, width=2)
            title = wrap(node.title, node.w * .75, TITLE_SIZE)
            body = wrap(node.body, node.w * .65, BODY_SIZE) if node.body else []
            total = len(title)*29 + (6+len(body)*23 if body else 0)
            y = cy-total/2-4 if node.inclusive else cy-total/2
            for line in title:
                self.text(cx, y, line, size=TITLE_SIZE, anchor="middle")
                y += 29
            if body:
                y += 6
                for line in body:
                    self.text(cx, y, line, anchor="middle")
                    y += 23
            if node.inclusive:
                self.circle(cx, node.y+node.h-22, 8, fill="#EBF3E9", stroke=GREEN)
        else:
            fill, title_color, body_color = (TEAL, "#A5E8AF", "#C9E7D9") if node.style == "done" else ("#FFFFFF", INK, INK)
            self.rect(node.x, node.y, node.w, node.h, fill=fill, stroke=None if node.style == "done" else "#DCDAD4")
            if node.style != "done":
                self.rect(node.x, node.y+13, 5, node.h-26, fill=edge, radius=2)
            title = wrap(node.title, node.w-32, TITLE_SIZE)
            body = wrap(node.body, node.w-32, BODY_SIZE) if node.body else []
            needed = 32+len(title)*29+(9+len(body)*23 if body else 0)
            if needed > node.h:
                raise ValueError(f"Node {node.key} needs {needed}px, has {node.h}px; edit wording or geometry, never reduce fonts")
            y = node.y+16
            for line in title:
                self.text(node.x+16, y, line, size=TITLE_SIZE, color=title_color)
                y += 29
            if body:
                y += 9
                for line in body:
                    self.text(node.x+16, y, line, color=body_color)
                    y += 23
        self.parts.append('</g>')
        self.group = None

    def save(self, out, stem):
        (out / (stem + ".svg")).write_text("\n".join(self.parts + ['</svg>'])+"\n", encoding="utf-8", newline="\n")
        self.image.save(out / (stem + ".png"))
        (out / (stem + ".scene.json")).write_text(json.dumps({"width": self.width, "height": self.height,
            "background": BACKGROUND, "font_family": "Segoe UI", "font_sizes": [TITLE_SIZE, BODY_SIZE],
            "primitives": self.primitives}, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def build(out, *, main_only=False):
    """Eight visible stages, two independent skills, optional branches explicit."""
    out.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(height=2260, width=3560)
    columns = [40 + i * 440 for i in range(8)]
    canvas.text(40, 26, "Перенос контекста и настройка нового агента под ключ", size=TITLE_SIZE)
    canvas.text(40, 65, "Версия 0.8.4 · Актуально на 25.11.2026 · Полный пакет для пользователя + формат получателя · Прежние решения сохраняются. Здесь — этапы работы агентов; в инструкции — шаги пользователя.")
    execution_legend(canvas, 40, 99)
    canvas.rect(40, 136, 2600, 42, fill=TEAL, radius=10)
    canvas.text(58, 144, "НАВЫК ЭКСПОРТА · memory-export · собирает, проверяет и готовит передачу", size=TITLE_SIZE, color="#A5E8AF")
    canvas.rect(2680, 136, 840, 42, fill=TEAL, radius=10)
    canvas.text(2698, 144, "ИМПОРТ · навык или инструкция получателя", size=TITLE_SIZE, color="#A5E8AF")
    titles = ["1. Начать", "2. Разобрать источники", "3. Выбрать и собрать", "4. Проверить профиль",
              "5. Подготовить формат", "6. Сохранить и передать", "7. Принять и сравнить", "8. Настроить и проверить"]
    subtitles = ["Явный старт, подходящий получатель", "Среда, данные, доступ и роль", "Выбранные ветки идут параллельно", "Все выбранные источники вместе",
                 "По возможностям получателя", "Конкретные файлы и папки", "Пакет и существующая память", "Только подтверждённый результат"]
    for x, title, subtitle in zip(columns, titles, subtitles):
        canvas.text(x + 8, 202, title, size=TITLE_SIZE, color=GREEN)
        canvas.text(x + 8, 242, subtitle, color=MUTED)
        canvas.rect(x, 282, 400, 1678, fill="#F0EAE6", radius=20)

    nodes = {}
    def box(key, col, y, h, title, body="", execution="model", style="normal"):
        nodes[key] = Node(key, columns[col]+16, y, 368, h, title, body, execution=execution, style=style)
    def diamond(key, col, cy, h, title, body="", width=324, inclusive=False, execution="model"):
        nodes[key] = Node(key, columns[col]+(400-width)/2, cy-h/2, width, h, title, body,
                          "diamond", inclusive=inclusive, execution=execution)

    box("request", 0, 315, 140, "Запустить экспорт", "Подключить или передать агенту\nполный комплект memory-export.\nПопросить начать перенос.")
    box("offer", 0, 500, 170, "Предложение переноса", "В подходящем текущем разговоре\nнавык сам предлагает начать.\nНовый чат или уведомление\nсамостоятельно не создаёт.")
    box("recipient", 0, 765, 220, "Уточнить получателя", "ГигаАгент; ГигаАгент Про\n(Ouroboros); ChatGPT; Claude;\nдругой агент. Учитываем допустимые\nфайлы и способы записи памяти.\nИзвестный выбор не повторяем.")
    box("resume", 0, 1060, 240, "Продолжить с учётом решений", "Используем прежние ответы,\nвыбранные источники и отказы.\nНе начинаем интервью заново.\nНовые факты, изменённые файлы\nи текущий доступ проверяем.\nРешения можно изменить.")

    box("source-map", 1, 315, 230, "Показать карту источников", "ChatGPT · Claude · Dots · Duet\nCodex · Claude Code\nСреда: веб, десктоп или CLI.\nОтдельно: аккаунт, рабочее\nпространство и набор данных.")
    box("source-locations", 1, 590, 280, "Уточнить данные и доступ", "История аккаунта; файлы в облаке;\nлокальные папки на компьютере.\nДесктоп может работать со всеми.\nОбщая история учитывается один\nраз, дополнительные файлы —\nотдельно. Проверяем, что агент\nдействительно может прочитать.")
    diamond("source-count", 1, 1030, 220, "Источников\nнесколько?", "После проверки состава")
    box("single-source", 1, 1200, 170, "Собрать один источник", "Агент сообщает: подготовлю\nэкспорт здесь за один проход.\nПроверка и уточнения сохраняются.")
    box("coordinator", 1, 1440, 245, "Назначить ведущего", "Агент объявляет свою роль:\nсвести источники, убрать повторы\nи проверить противоречия.\nОстальные передают материалы;\nнавык в каждом источнике\nустанавливать необязательно.")

    diamond("scope", 2, 410, 210, "Что переносим?", "Можно сочетать", inclusive=True)
    box("profile", 2, 580, 210, "Профиль и инструкции", "Почты, предпочтения, правила.\nПользовательские инструкции\nи контекст Dots / Duet: проекты,\nобязанности, задачи, подключения.\nСохраняем источники и область.")
    box("works", 2, 845, 250, "Предложить работы → навыки", "Агент сам находит полезные\nрезультаты в доступных беседах\nи показывает кандидатов.\nПользователь выбирает. Избранное\nсохраняем как материалы или\nсоздаём и проверяем новый навык.")
    box("skills", 2, 1150, 210, "Сохранить готовые навыки", "Оригиналы, код и шаблоны,\nв том числе самописные.\nПравила проекта включаем\nв связанную копию навыка.\nЗависимости отмечаем отдельно.")
    box("history", 2, 1415, 220, "Получить выбранные архивы", "Готовое обрабатываем сразу.\nПользователь передаёт то,\nк чему агент не имеет доступа.\nОжидание не останавливает другие ветки.")
    box("new-source", 2, 1720, 195, "Появились новые данные", "Добавлен источник или изменился\nего состав. Обновляем общий\nпрофиль и повторяем проверку.\nПрежние действительные решения\nсохраняются.", execution="code")

    box("common-review", 3, 315, 295, "Сверить общий профиль", "Модель сравнивает факты,\nсмысловые дубли, противоречия\nи актуальность по источникам.\nЛичное и рабочее не смешивает.\nВторой проход проверяет выводы.\nКод проверяет охват, состояние\nи актуальность отчёта.")
    diamond("disputed", 3, 775, 220, "Есть спорное?", "Неясное или устаревшее")
    box("clarify", 3, 955, 190, "Уточнить только спорное", "Показываем найденное и основания.\nЗадаём точечный вопрос.\nДата импорта не делает старое\nсведение актуальным.")
    box("decisions", 3, 1230, 235, "Зафиксировать решения", "Сохраняем согласованный смысл\nи область действия.\nПовторы показываем один раз,\nисточники и оригиналы остаются.\nНеуточнённое отделяем\nот активной памяти.", execution="code")
    diamond("export-gate", 3, 1670, 230, "Проверка\nактуальна?", "Общий состав и решения", execution="code")

    diamond("target", 4, 410, 210, "Какой\nполучатель?", "Выбран на старте")
    box("giga-format", 4, 580, 340, "ГигаАгент: подготовить MD", "Разделить смешанные записи.\nПоказать: войдёт / не войдёт /\nнужно уточнить — и причины.\nuser ≤ 20 · feedback ≤ 30\nproject ≤ 100 · reference ≤ 40\nОписание ≤ 200, факт ≤ 450 знаков.\nВременное, спорное и избыток\nостаются в полном ZIP пользователя.\nИсключённые записи убираем.\nСостав исходников проверяем отдельно.")
    box("pro-format", 4, 950, 215, "ГигаАгент Про (Ouroboros)", "Ouroboros → Skills →\nAmberkeep: carry memory.\nУстановить и включить навык,\nприложить выгрузку и начать импорт.\nВ живом аккаунте не проверено.", execution="tools")
    box("other-format", 4, 1200, 195, "Другие получатели", "Для выбранного ChatGPT / Claude\nприлагается memory-import.zip.\nБез него сборка не завершится.\nДругим — подходящие файлы.\nУстановка зависит от получателя.", execution="code")
    diamond("obsidian", 4, 1540, 210, "Нужен\nObsidian?", "Спросить, если неизвестно")
    box("vault", 4, 1740, 200, "Подготовить папку со связями", "Для существующего хранилища\nили нового набора Markdown.\nObsidian необязателен; платная\nсинхронизация не требуется.", execution="code")

    box("save-choice", 5, 315, 290, "Согласовать сохранение", "Предложить папку «Перенос памяти».\nКомпьютер, скачивание в чате;\nGoogle, Яндекс, Mail, OneDrive\nили другое облако. Полный ZIP:\nпрофиль, правила, проекты,\nматериалы и навыки. Для ГигаАгента\nещё два MD. Лимиты его памяти\nне сокращают полный пакет.")
    diamond("write-access", 5, 750, 220, "Есть доступ\nдля записи?", "Проверен инструментом", execution="tools")
    box("save-tool", 5, 930, 190, "Сохранить инструментом", "Записать файлы и проверить их.\nФайл в папке синхронизации\nещё не доказывает загрузку в облако.", execution="tools")
    box("save-user", 5, 1160, 220, "Выдать файлы в чате", "ГигаАгент: полный ZIP пользователю\n+ два отдельных MD для загрузки.\nОстальным — комплект получателя.\nВложить файлы или рабочие ссылки\nи сообщение запуска импорта.\nКод готовит; агент выдаёт в чате.", execution="tools")
    diamond("shared-folder", 5, 1510, 220, "Общая папка\nдоступна обоим?", execution="tools")
    box("copy", 5, 1710, 245, "Передать и запустить приём", "Передать: вложения в чат,\nлокальные файлы или облако.\nГигаАгенту — только два MD:\nclient-memory-export.md\nи import-instruction.md; ZIP себе.\nЗатем отправить сообщение импорта.")

    box("inspect", 6, 315, 275, "Проверить входные материалы", "Приём по сообщению пользователя.\nГигаАгент: прочитать оба MD целиком;\nприменить штатные инструменты\nпамяти, без Python-импортера.\nZIP и навыки он не принимает.\nПакетный импорт: проверить состав,\nцелостность, версию и получателя.", execution="code")
    box("compare", 6, 630, 270, "Прочитать текущую память", "Нужен снимок памяти получателя.\nСтарая рабочая папка его не заменяет.\nБез снимка сохраняем файлы,\nно блокируем активный профиль\nи подтверждение нативной памяти.\nСо снимком: сверить дубли,\nдополнения, конфликты и смысл\nнового объединённого профиля.")
    diamond("can-apply", 6, 1040, 240, "Можно\nприменять?", "Сверка и изменения согласованы")
    box("resolve", 6, 1260, 230, "Устранить препятствия", "Память неизвестна — получить снимок.\nНовый конфликт — точечный вопрос.\nПроверить объединённый профиль.\nПрежние ответы и отложенные шаги\nсохранить; интервью не повторять.\nСпорные записи не активировать.")
    box("giga-import", 6, 1570, 345, "Правила записи ГигаАгента", "Только «Готово к импорту».\nmemory_list с архивными записями;\nсовпадения прочитать memory_read.\nЛимиты с учётом занятой памяти:\nбез вытеснения и обхода категорий.\nmemory_save — отдельно для факта,\n≤ 500 знаков с переводом строки.\nПри замене — точная версия записи.\nПроцедуры не объявлять knowledge.", execution="tools")

    box("setup-scope", 7, 315, 275, "Зафиксировать объём настройки", "Для пакетного импорта фиксируем\nполучателя и выбранные разделы.\nЯвно отложенное вне этого объёма\nне мешает завершению выбранного.\nПовторное интервью не требуется.\nНерешённое внутри объёма\nостаётся задачей, а не успехом.", execution="code")
    box("configure", 7, 630, 295, "Выполнить доступную настройку", "Сохранить согласованные сведения,\nинструкции и проектный контекст.\nУстановить выбранные навыки,\nподключить нужные ресурсы\nи расписания — только если\nполучатель это поддерживает.\nИмя продукта не даёт инструментов.", execution="tools")
    box("readback", 7, 1010, 225, "Проверить фактический результат", "Прочитать записанную память,\nпроверить файлы и выполненные\nнастройки. Частичный сбой\nповторять с учётом уже сделанного,\nбез слепого повторного импорта.", execution="tools")
    diamond("done-check", 7, 1400, 230, "Всё выбранное\nпроверено?", execution="tools")
    box("done", 7, 1595, 150, "Готово в выбранном объёме", "Новый агент использует\nпроверенный перенесённый контекст.", execution="tools", style="done")
    box("remaining", 7, 1770, 185, "Выдать итог и план", "При пакетном импорте обязательны\nIMPORT_REPORT.md и NEXT_STEPS.md.\nГигаАгент: результат в чате.\nОшибки и остаток — явным списком.", execution="code")

    labels = []
    def label(x, y, value):
        labels.append((x, y, value))
    def down(a, b, dashed=False):
        canvas.path([nodes[a].bottom, nodes[b].top], dashed=dashed)
    def side(a, b, x, *, left=True, dashed=False):
        start, end = (nodes[a].left, nodes[b].left) if left else (nodes[a].right, nodes[b].right)
        canvas.path([start, (x,start[1]), (x,end[1]), end], dashed=dashed)
    def across(a, b, *, channel=None, dashed=False):
        start, end = nodes[a].right, nodes[b].left
        middle = channel if channel is not None else (start[0]+end[0])/2
        canvas.path([start, (middle,start[1]), (middle,end[1]), end], dashed=dashed)
    def merge_right(keys, b, x):
        endpoint = nodes[b].left
        for key in keys:
            start = nodes[key].right
            canvas.path([start, (x,start[1]), (x,endpoint[1])], arrow=False)
        canvas.circle(x, endpoint[1], 4, fill=LINE)
        canvas.path([(x,endpoint[1]), endpoint])

    # Alternative current-session entry points.
    merge_y = 715
    canvas.path([nodes["request"].right, (columns[0]+394,nodes["request"].right[1]),
                 (columns[0]+394,merge_y), (nodes["recipient"].top[0],merge_y)], arrow=False)
    canvas.path([nodes["offer"].bottom, (nodes["recipient"].top[0],merge_y)], arrow=False)
    canvas.circle(nodes["recipient"].top[0], merge_y, 4, fill=LINE)
    canvas.path([(nodes["recipient"].top[0],merge_y), nodes["recipient"].top])
    down("recipient", "resume")
    across("resume", "source-map")
    down("source-map", "source-locations")
    down("source-locations", "source-count")
    down("source-count", "single-source")
    label(nodes["source-count"].bottom[0]+10, 1152, "Один")
    side("source-count", "coordinator", columns[1]+5)
    label(columns[1]+8, 1150, "Несколько")
    merge_right(["single-source", "coordinator"], "scope", columns[1]+415)

    # Inclusive collection: selected branches, not four mandatory interviews.
    fork_x, bus_x = columns[2]+5, columns[2]+393
    canvas.path([nodes["scope"].bottom, (nodes["scope"].bottom[0],546), (fork_x,546),
                 (fork_x,nodes["history"].left[1])], arrow=False)
    for key in ("profile", "works", "skills", "history"):
        canvas.path([(fork_x,nodes[key].left[1]), nodes[key].left])
        canvas.path([nodes[key].right, (bus_x,nodes[key].right[1])], arrow=False)
    join_y = nodes["common-review"].left[1]
    canvas.path([(bus_x,join_y), (bus_x,nodes["history"].right[1])], arrow=False)
    canvas.circle(bus_x, join_y, 5, fill=LINE)
    canvas.path([(bus_x,join_y), nodes["common-review"].left])
    down("common-review", "disputed")
    down("disputed", "clarify")
    label(nodes["disputed"].bottom[0]+10, 899, "Да")
    side("disputed", "decisions", columns[3]+5)
    label(columns[3]+8, 905, "Нет")
    down("clarify", "decisions")
    down("decisions", "export-gate")
    across("export-gate", "target")
    label(nodes["export-gate"].right[0]+8, nodes["export-gate"].right[1]-31, "Да")
    canvas.path([nodes["export-gate"].bottom, (nodes["export-gate"].bottom[0],1968),
                 (columns[3]+394,1968), (columns[3]+394,nodes["common-review"].right[1]),
                 nodes["common-review"].right], dashed=True)
    label(nodes["export-gate"].bottom[0]+10, 1800, "Нет: сверить")

    # Target is already known; this decision selects its correct output.
    target_x, merge_x = columns[4]+5, columns[4]+394
    canvas.path([nodes["target"].bottom, (nodes["target"].bottom[0],545), (target_x,545),
                 (target_x,nodes["other-format"].left[1])], arrow=False)
    for key in ("giga-format", "pro-format", "other-format"):
        canvas.path([(target_x,nodes[key].left[1]), nodes[key].left])
        canvas.path([nodes[key].right, (merge_x,nodes[key].right[1])], arrow=False)
    canvas.path([(merge_x,nodes["giga-format"].right[1]), (merge_x,1400), (nodes["obsidian"].top[0],1400), nodes["obsidian"].top])
    down("obsidian", "vault")
    label(nodes["obsidian"].bottom[0]+10, 1662, "Да")
    across("obsidian", "save-choice", channel=columns[4]+418)
    label(nodes["obsidian"].right[0]+8, nodes["obsidian"].right[1]-29, "Нет / позже")
    across("vault", "save-choice", channel=columns[4]+425)
    down("save-choice", "write-access")
    down("write-access", "save-tool")
    label(nodes["write-access"].bottom[0]+10, 872, "Да")
    side("write-access", "save-user", columns[5]+5)
    label(columns[5]+8, 878, "Нет")
    # Every route hands the user real files or verified download links in chat.
    down("save-tool", "save-user")
    down("save-user", "shared-folder")
    down("shared-folder", "copy")
    label(nodes["shared-folder"].bottom[0]+10, 1634, "Нет")
    across("shared-folder", "inspect", channel=columns[5]+413)
    label(nodes["shared-folder"].right[0]+8, nodes["shared-folder"].right[1]-29, "Да")
    across("copy", "inspect", channel=columns[5]+425)

    down("inspect", "compare")
    down("compare", "can-apply")
    down("can-apply", "resolve")
    label(nodes["can-apply"].bottom[0]+10, 1173, "Нет")
    across("can-apply", "setup-scope", channel=columns[6]+416)
    label(nodes["can-apply"].right[0]+8, nodes["can-apply"].right[1]-29, "Да")
    # Native Giga contract is a callout attached to the permitted application route.
    canvas.path([nodes["giga-import"].right, (columns[6]+425,nodes["giga-import"].right[1]),
                 (columns[6]+425,nodes["configure"].left[1]), nodes["configure"].left], dashed=True)
    down("setup-scope", "configure")
    down("configure", "readback")
    down("readback", "done-check")
    down("done-check", "done")
    label(nodes["done-check"].bottom[0]+10, 1538, "Да")
    side("done-check", "remaining", columns[7]+5)
    label(columns[7]+8, 1550, "Нет")
    down("done", "remaining")

    # Both changed source data and an unchecked receiving union return to one review.
    return_x = columns[3]+5
    canvas.path([nodes["new-source"].bottom, (nodes["new-source"].bottom[0],2002),
                 (return_x,2002)], arrow=False, dashed=True)
    canvas.path([nodes["resolve"].left, (columns[6]+5,nodes["resolve"].left[1]),
                 (columns[6]+5,2038), (return_x,2038), (return_x,join_y), nodes["common-review"].left], dashed=True)
    canvas.circle(return_x, 2002, 4, fill=LINE)
    label(1495, 2008, "Новые данные или новое объединение → общая проверка; прежние действительные решения сохраняются")

    for node in nodes.values():
        canvas.node(node)
    for x, y, text in labels:
        canvas.text(x, y, text, backing=True)
    canvas.rect(40, 2080, 3480, 85, fill=GRAY, radius=14)
    canvas.text(58, 2094, "Ограничения текущего сценария", size=TITLE_SIZE, color="#40504D")
    canvas.text(58, 2131, "Почта и фоновый мониторинг · Самостоятельные уведомления между агентами · Нативный импорт ZIP и навыков в обычном ГигаАгенте · MCP Obsidian и Яндекс Диска", color="#485652")
    canvas.text(58, 2180, "Пакетный импорт всегда сохраняет IMPORT_REPORT.md и NEXT_STEPS.md. Сохранённые файлы не означают, что память получателя настроена.", color=MUTED)
    canvas.text(58, 2212, "Код проверен на предоставленных файлах и готовит результаты. Файлы в чат выдаёт агент доступными инструментами; выдача и нативный SaaS-импорт в реальном аккаунте ещё не проверены.", color=MUTED)
    canvas.save(out, "user-journey")
    if not main_only:
        build_context_detail(out)
    print(json.dumps({"svg": str((out / 'user-journey.svg').resolve()), "preview": str((out / 'user-journey.png').resolve()),
                      "version": "0.8.4", "nodes": len(nodes), "font_sizes": [TITLE_SIZE, BODY_SIZE],
                      "dimensions": [canvas.width, canvas.height]}, ensure_ascii=False))


def execution_legend(canvas, x, y):
    for label, key in (("Код: проверки и файлы", "code"), ("Модель и пользователь", "model"), ("Инструменты текущего агента", "tools")):
        canvas.rect(x, y+1, 5, 18, fill=EXECUTION_COLORS[key], radius=2)
        canvas.text(x+13, y, label, color=EXECUTION_COLORS[key])
        x += measure(label)+48


def build_context_detail(out):
    canvas = Canvas()
    canvas.text(40, 28, "Dots и Duet: состав контекста и исполнение", size=TITLE_SIZE)
    canvas.text(40, 70, "Поле в схеме данных не подтверждает доступ к сервису. Сначала проверяем, что реально можно прочитать, затем сохраняем и согласуем содержание.")
    execution_legend(canvas, 40, 105)
    columns = [40, 910, 1780]
    headings = ["Что забрать из доступного источника", "Как сохранить инструкции и связи", "Что выполняет каждая часть решения"]
    for x, heading in zip(columns, headings):
        canvas.text(x+8, 170, heading, size=TITLE_SIZE, color=GREEN)
        canvas.rect(x, 224, 790, 1236, fill="#F0EAE6", radius=20)
    entries = [
        ("notes", 0, 270, 220, "Заметки и явные инструкции", "Сохранённые заметки, сведения о пользователе, предпочтения.\nОтдельно — дословные пользовательские инструкции.\nУ каждой записи есть источник, дата и область действия.", "model"),
        ("projects", 0, 550, 220, "Проекты, правила и обязанности", "Описания проектов, их статус, решения и проектные инструкции.\nРоли пользователя и постоянные обязанности.\nОбщие правила не подменяют правила отдельного проекта.", "model"),
        ("tasks", 0, 830, 240, "Текущие и регулярные задачи", "Для текущих: состояние, срок, ответственный, следующий шаг.\nДля регулярных: периодичность, время, часовой пояс и условия.\nСохранённое описание задачи не создаёт расписание у получателя.", "model"),
        ("resources", 0, 1130, 250, "Подключения, каналы и артефакты", "Почты, календари, диски, рабочие каналы и связанные аккаунты.\nДокументы, результаты работы, оригиналы файлов и их связи.\nПереносим описание настройки, правила работы и доступные файлы.\nПароли, токены и право доступа повторно не выдаём через пакет.", "model"),
        ("verbatim", 1, 270, 240, "Исходная инструкция сохраняется", "Пользовательская инструкция хранится отдельной записью.\nИсходный текст сохраняем дословно вместе с источником.\nКраткое описание может быть рядом, но не заменяет оригинал.\nАктуальность и противоречия подтверждает пользователь.", "model"),
        ("binding", 1, 570, 260, "Правило проекта связано с навыком", "Фиксируем проект, навык или задачу, к которым относится правило.\nСохраняем исходный текст, область действия и источник.\nВ новом навыке указываем эту связь и нужные зависимости.\nПринимающий агент видит, где применять правило и его исключения.", "model"),
        ("example", 1, 890, 240, "Пример сохранения области действия", "«Для проекта Альфа используй шаблон отчёта из этой папки».\nЭто правило проекта Альфа и связанного навыка отчётности.\nЕго нельзя автоматически сделать правилом всех проектов.\nЕсли папка недоступна, зависимость остаётся видимым пробелом.", "model"),
        ("gaps", 1, 1190, 190, "Неполные данные остаются видимыми", "Отмечаем прочитанное, недоступное и отложенное пользователем.\nНовые противоречия уточняем точечно.\nСохраняем прошлые решения и продолжаем доступные ветки.", "code"),
        ("code", 2, 270, 240, "Код: работа с предоставленными файлами", "Параллельный сбор выбранных файлов и проверка структуры.\nКонтроль согласований, упаковка, Markdown, вики и связи.\nПроверка передачи, сравнение записей и сохранение продолжения.\nЭти проверки сами не читают весь аккаунт Dots или Duet.", "code"),
        ("model", 2, 570, 240, "Модель и пользователь: смысл и выбор", "Извлечение и смысловая проверка сведений по источникам.\nВыбор ценных работ, подготовка новых навыков.\nСогласование актуальности, конфликтов и области инструкций.\nМодель не заменяет пользовательское подтверждение.", "model"),
        ("tools", 2, 870, 260, "Инструменты сервиса: фактические действия", "Чтение кабинета, подключённых данных и облачных файлов.\nЗагрузка, установка навыков, настройка памяти и расписаний.\nВыполняем только поддерживаемые и разрешённые действия.\nПосле применения читаем результат обратно.\nУниверсальный прямой доступ к Dots/Duet пока не подтверждён.", "tools"),
        ("honest-finish", 2, 1190, 190, "Итог по каждой выбранной части", "Есть проверенный результат — отмечаем готовность.\nНет доступа или подтверждения — сохраняем следующий шаг.\nПолученный архив сам по себе не означает настройку агента.", "tools"),
    ]
    for key, col, y, h, title, body, execution in entries:
        canvas.node(Node(key, columns[col]+20, y, 750, h, title, body, execution=execution))
    canvas.rect(40, 1500, WIDTH-80, 112, fill=GRAY, radius=14)
    canvas.text(58, 1514, "Серым: недоступное и отложенное явно остаётся в плане", size=TITLE_SIZE, color="#40504D")
    canvas.text(58, 1553, "Почта и фоновое наблюдение, универсальные облачные адаптеры, нативный импорт навыков и ZIP в ГигаАгенте, MCP Obsidian и Яндекс Диска.", color="#485652")
    canvas.text(58, 1582, "Схема описывает целевой путь. Реальная готовность определяется доступными инструментами сервиса и проверкой результата каждой выбранной операции.", color="#485652")
    canvas.save(out, "dots-duet-context")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "docs" / "diagrams")
    parser.add_argument("--main-only", action="store_true", help="Update only the main journey, preserving the separate context diagram")
    args = parser.parse_args()
    build(args.out, main_only=args.main_only)
