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
    def __init__(self, height=HEIGHT):
        self.height = height
        self.image = Image.new("RGB", (WIDTH, height), BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)
        self.primitives = []
        self.group = None
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="diagram-title diagram-desc">',
                      '<title id="diagram-title">Путь переноса памяти и настройки другого агента</title>',
                      '<desc id="diagram-desc">Семь этапов слева направо. Один ведущий агент собирает общий профиль из выбранных источников. Перед экспортом обязательна общая смысловая проверка и второй проход. Новые данные и непроверенное объединение с памятью получателя возвращаются на общую проверку, с сохранением решений пользователя.</desc>',
                      '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L8,4 L0,8 Z" fill="' + LINE + '"/></marker></defs>',
                      f'<rect width="{WIDTH}" height="{height}" fill="{BACKGROUND}"/>']

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
        (out / (stem + ".svg")).write_text("\n".join(self.parts + ['</svg>'])+"\n", encoding="utf-8")
        self.image.save(out / (stem + ".png"))
        (out / (stem + ".scene.json")).write_text(json.dumps({"width": WIDTH, "height": self.height,
            "background": BACKGROUND, "font_family": "Segoe UI", "font_sizes": [TITLE_SIZE, BODY_SIZE],
            "primitives": self.primitives}, ensure_ascii=False, indent=2), encoding="utf-8")


def build(out, *, main_only=False):
    out.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(height=1760)
    columns = [40+i*365 for i in range(7)]
    stages = ["1. Начать", "2. Выбрать и собрать", "3. Проверить", "4. Упаковать", "5. Передать", "6. Принять", "7. Настроить"]
    subtitles = ["Один ведущий, несколько источников", "Четыре совместимые ветки", "Единый профиль всех источников", "Только проверенная версия", "Доступ и ручное копирование", "Пакет и собственная память", "В выбранном объёме"]
    canvas.text(40, 28, "Путь переноса памяти и настройки другого агента", size=TITLE_SIZE)
    canvas.text(40, 70, "Версия 0.7 · Ведущий агент проводит общую смысловую проверку. Код проверяет полноту и актуальность её отчёта. Настройка сервиса — отдельный шаг.")
    execution_legend(canvas, 40, 99)
    for x, title, subtitle in zip(columns, stages, subtitles):
        canvas.text(x+8, 126, title, size=TITLE_SIZE, color=GREEN)
        for line_no, line in enumerate(wrap(subtitle, 324, BODY_SIZE)):
            canvas.text(x+8, 164+line_no*23, line, color=MUTED)
        canvas.rect(x, 214, 340, 1260, fill="#F0EAE6", radius=20)

    nodes = {}
    def box(key, col, y, h, title, body="", style="normal"):
        nodes[key] = Node(key, columns[col]+16, y, 308, h, title, body, style=style)
    def diamond(key, col, cy, h, title, body="", width=276, inclusive=False):
        nodes[key] = Node(key, columns[col]+(340-width)/2, cy-h/2, width, h, title, body, "diamond", inclusive=inclusive)

    box("start-user", 0, 265, 120, "Запрос пользователя", "Хочу перенести память\nиз другого агента.")
    box("start-cross", 0, 430, 145, "Предложение Кросса", "Внешний триггер продукта.\nНавык сам новый диалог\nне начинает.")
    box("coordinator", 0, 620, 220, "Выбрать ведущего агента", "Он собирает общий профиль,\nведёт диалог и хранит решения.\nВ остальных источниках навык\nне обязателен: можно передать\nготовые файлы.")
    box("source-map", 0, 880, 230, "Карта источников", "ChatGPT · Claude · Dots · Duet\nClaude Code · Codex\nОдин аккаунт и набор данных\nв разных приложениях —\nодин источник. Проектные\nпапки учитываем отдельно.")
    box("dots-duet-context", 0, 1140, 320, "Контекст Dots / Duet", "Заметки и явные инструкции;\nпроекты и правила проектов;\nобязанности, текущие\nи регулярные задачи;\nподключения, каналы, артефакты.\nСохраняем доступные данные,\nисходные инструкции и их связи.\nПробелы остаются видимыми.")

    diamond("scope-choice", 1, 300, 174, "Что переносим?", "Можно сочетать", inclusive=True)
    box("collect-profile", 1, 440, 174, "Профиль и инструкции", "Заметки, почты, предпочтения.\nИнструкции дословно,\nс источником и областью.")
    box("collect-works", 1, 650, 200, "Работы и новые навыки", "Правила проекта связываем\nс навыком: исходный текст,\nобласть действия и источник.\nОтбираем значимые результаты.")
    box("collect-skills", 1, 860, 174, "Готовые навыки", "В том числе самописные.\nОригиналы с кодом и шаблонами.\nСписок зависимостей.")
    box("collect-history", 1, 1070, 190, "Полные архивы", "Готовые обрабатываем сразу.\nОжидаемые приносит\nпользователь. Другие ветки\nпродолжаются сейчас.")

    box("new-source", 1, 1300, 174, "Добавился источник", "Или изменились его данные.\nПрежняя проверка устарела.\nОбновляем общий профиль\nи проверяем заново.")

    box("quality-check", 2, 265, 345, "Проверить общий профиль", "Ведущий сравнивает все\nпредоставленные источники:\nсмысловые дубли, противоречия,\nактуальность и исходные цитаты.\nЛичное, рабочее и проектное\nразличает по области действия.\nВторой проход проверяет выводы.")
    diamond("quality-decision", 2, 750, 190, "Есть спорное?", "Или устаревшее", width=254)
    box("quality-clarify", 2, 900, 190, "Уточнить спорное", "Только новое и изменившееся.\nБолее поздняя запись\nне получает приоритет.\nПолное интервью не повторяем.")
    box("quality-confirm", 2, 1120, 300, "Сохранить решения", "Новое подтверждает пользователь.\nПрежние актуальные ответы\nи отложения сохраняются.\nПовторы показываем один раз;\nоригиналы и источники остаются.\nСпорное и неподтверждённое\nне активируем.")

    box("choose-storage", 3, 280, 151, "Выбрать хранилище", "Google Диск, Яндекс Диск\nили другое доступное место.\nУ получателя может быть другое.")
    diamond("export-gate", 3, 595, 250, "Проверка\nактуальна?", "Для всех источников", width=324)
    box("portable-package", 3, 770, 280, "Подготовить пакет", "Код допускает только\nпроверенную версию.\nПапка, карточка переноса,\nотчёт проверки, MD / вики,\nоригиналы и связи. Неполный\nперенос — в согласованном\nс пользователем объёме.")
    diamond("write-access", 3, 1210, 196, "Есть доступ\nна запись?", "У отправителя", width=282)

    box("host-upload", 4, 435, 120, "Загружает агент", "Через действительно\nдоступный инструмент.")
    box("user-upload", 4, 620, 145, "Загружает пользователь", "Получает готовую папку\nи короткую инструкцию.")
    box("version-folder", 4, 830, 170, "Пакет размещён", "Комплект в выбранном\nхранилище отправителя.\nДалее — доступ получателя.")
    diamond("shared-access", 4, 1110, 190, "Общая папка?", "Доступ проверен", width=290)
    box("user-copy", 4, 1240, 125, "Пользователь копирует", "В папку выбранного получателя.")
    box("recipient-open", 4, 1380, 100, "Получатель открывает", "После сообщения пользователя.")

    box("verify-delivery", 5, 350, 174, "Проверить комплект", "Файлы и контрольные суммы.\nВерсия и карточка передачи.\nНужный агент и его пространство.")
    diamond("copy-ok", 5, 710, 170, "Копия целая?", "Всё совпало", width=254)
    box("repair-copy", 5, 875, 120, "Исправить копию", "Докопировать или передать\nнеизменённый комплект заново.")
    diamond("memory-conflict", 5, 1135, 240, "Объединение\nпроверено?", "Пакет + своя память", width=324)
    box("resolve-conflict", 5, 1300, 174, "Вернуть ведущему", "Объединённый профиль —\nна общую проверку.\nДо её завершения применение\nи настройка заблокированы.")

    box("apply-selected", 6, 540, 251, "Настроить получателя", "Профиль и проекты.\nНавыки и подключения.\nПри необходимости — задачи\nпо расписанию.\nТолько через доступные\nинструменты принимающего агента.")
    diamond("setup-verified", 6, 1020, 210, "Все выбранные\nшаги проверены?", width=300)
    box("ready", 6, 1200, 145, "Готово в выбранном объёме", "Проверенные настройки\nготовы к работе.", style="done")
    box("remaining", 6, 1360, 120, "Что осталось сделать", "Непроверенные шаги.\nЧто нужно сделать дальше.")
    for key in ("collect-skills", "collect-history", "new-source", "export-gate", "portable-package", "verify-delivery", "copy-ok", "memory-conflict", "remaining"):
        nodes[key].execution = "code"
    for key in ("start-cross", "write-access", "host-upload", "version-folder", "shared-access", "recipient-open", "apply-selected", "setup-verified", "ready"):
        nodes[key].execution = "tools"

    labels = []
    def link(a, b, *, channel=None, dashed=False):
        start, end = nodes[a].right, nodes[b].left
        middle = channel if channel is not None else (start[0]+end[0])/2
        canvas.path([start, (middle, start[1]), (middle, end[1]), end], dashed=dashed)
    def down(a, b):
        start, end = nodes[a].bottom, nodes[b].top
        canvas.path([start, (start[0], end[1])])
    def bypass(a, b, x, *, side="left"):
        start = nodes[a].left if side == "left" else nodes[a].right
        end = nodes[b].left if side == "left" else nodes[b].right
        canvas.path([start, (x, start[1]), (x, end[1]), end])
    def label(x, y, value):
        labels.append((x, y, value))

    # Two actual entry points; the skill itself creates no proactive session.
    start = nodes["start-user"].right
    x = columns[0]+334
    merge_y = 590
    canvas.path([start, (x,start[1]), (x,merge_y), (nodes["coordinator"].top[0],merge_y)], arrow=False)
    canvas.path([nodes["start-cross"].bottom, (nodes["coordinator"].top[0],merge_y)], arrow=False)
    canvas.circle(nodes["coordinator"].top[0], merge_y, 4, fill=LINE)
    canvas.path([(nodes["coordinator"].top[0],merge_y), nodes["coordinator"].top])
    down("coordinator", "source-map")
    link("source-map", "scope-choice")
    canvas.path([nodes["source-map"].bottom, nodes["dots-duet-context"].top], dashed=True, arrow=False)

    # Inclusive choice: selected, already available sources proceed independently.
    fork_x, bus_x = columns[1]+6, columns[1]+334
    canvas.path([nodes["scope-choice"].bottom, (nodes["scope-choice"].bottom[0],410), (fork_x,410), (fork_x,nodes["collect-history"].left[1])], arrow=False)
    for key in ("collect-profile", "collect-works", "collect-skills", "collect-history"):
        canvas.path([(fork_x,nodes[key].left[1]), nodes[key].left])
        canvas.path([nodes[key].right, (bus_x,nodes[key].right[1])], arrow=False, dashed=key=="collect-history")
    canvas.path([(bus_x,nodes["quality-check"].left[1]), (bus_x,nodes["collect-skills"].right[1])], arrow=False)
    canvas.path([(bus_x,nodes["collect-skills"].right[1]), (bus_x,nodes["collect-history"].right[1])], arrow=False, dashed=True)
    join_x, join_y = columns[1]+353, nodes["quality-check"].left[1]
    canvas.path([(bus_x,join_y), (join_x-8,join_y)], arrow=False)
    canvas.circle(join_x, join_y, 8, stroke=GREEN)
    canvas.path([(join_x+8,join_y), nodes["quality-check"].left])

    down("quality-check", "quality-decision")
    down("quality-decision", "quality-clarify")
    label(nodes["quality-decision"].bottom[0]+9, nodes["quality-decision"].bottom[1]+17, "Да")
    bypass("quality-decision", "quality-confirm", columns[2]+5)
    label(columns[2]+8, nodes["quality-decision"].left[1]-33, "Нет")
    down("quality-clarify", "quality-confirm")
    link("quality-confirm", "choose-storage")

    down("choose-storage", "export-gate")
    down("export-gate", "portable-package")
    label(nodes["export-gate"].bottom[0]+9, nodes["export-gate"].bottom[1]+12, "Да")
    start, end = nodes["export-gate"].left, nodes["quality-check"].top
    canvas.path([start, (columns[2]+335,start[1]), (columns[2]+335,234), (end[0],234),end], dashed=True)
    label(nodes["export-gate"].left[0]-38, nodes["export-gate"].left[1]-31, "Нет")
    down("portable-package", "write-access")
    link("write-access", "host-upload", channel=columns[3]+347)
    label(nodes["write-access"].right[0]+6, nodes["write-access"].right[1]-30, "Да")
    canvas.path([nodes["write-access"].bottom, (nodes["write-access"].bottom[0],1360),
                 (columns[3]+357,1360), (columns[3]+357,nodes["user-upload"].left[1]), nodes["user-upload"].left])
    label(nodes["write-access"].bottom[0]+9, nodes["write-access"].bottom[1]+15, "Нет")
    # Both publication branches meet visibly BEFORE the shared-access choice.
    # Separate this bus from the later common-folder/copy branch on the right.
    upload_merge = (nodes["version-folder"].top[0], 800)
    start = nodes["host-upload"].right
    canvas.path([start, (columns[4]+334,start[1]), (columns[4]+334,upload_merge[1]), upload_merge], arrow=False)
    canvas.path([nodes["user-upload"].bottom, upload_merge], arrow=False)
    canvas.circle(*upload_merge, 4, fill=LINE)
    canvas.path([upload_merge, nodes["version-folder"].top])
    down("version-folder", "shared-access")
    down("shared-access", "user-copy")
    label(nodes["shared-access"].bottom[0]+9, nodes["shared-access"].bottom[1]+15, "Нет")
    bypass("shared-access", "recipient-open", columns[4]+334, side="right")
    label(nodes["shared-access"].right[0]+6, nodes["shared-access"].right[1]-31, "Да")
    down("user-copy", "recipient-open")
    link("recipient-open", "verify-delivery")

    down("verify-delivery", "copy-ok")
    down("copy-ok", "repair-copy")
    label(nodes["copy-ok"].bottom[0]+9, nodes["copy-ok"].bottom[1]+17, "Нет")
    bypass("repair-copy", "verify-delivery", columns[5]+5)
    start = nodes["copy-ok"].right
    end = nodes["memory-conflict"].top
    canvas.path([start, (columns[5]+334,start[1]), (columns[5]+334,end[1]-10), (end[0],end[1]-10),end])
    label(nodes["copy-ok"].right[0]+6, nodes["copy-ok"].right[1]-31, "Да")
    down("memory-conflict", "resolve-conflict")
    label(nodes["memory-conflict"].bottom[0]+9, nodes["memory-conflict"].bottom[1]+15, "Нет")
    link("memory-conflict", "apply-selected", channel=columns[5]+349)
    label(nodes["memory-conflict"].right[0]+5, nodes["memory-conflict"].right[1]-31, "Да")

    # Both new input and an unreviewed recipient union re-enter the same review.
    # Existing confirmations remain valid where content and scope are unchanged.
    return_x, new_y, union_y = join_x, 1510, 1550
    canvas.path([nodes["new-source"].bottom, (nodes["new-source"].bottom[0],new_y),
                 (return_x,new_y)], arrow=False, dashed=True)
    canvas.path([nodes["resolve-conflict"].bottom, (nodes["resolve-conflict"].bottom[0],union_y),
                 (return_x,union_y), (return_x,join_y+8)], dashed=True)
    canvas.circle(return_x, new_y, 4, fill=LINE)
    label(1020, 1516, "Общая проверка объединения → новая проверенная версия пакета")

    down("apply-selected", "setup-verified")
    down("setup-verified", "ready")
    label(nodes["setup-verified"].bottom[0]+9, nodes["setup-verified"].bottom[1]+18, "Да")
    bypass("setup-verified", "remaining", columns[6]+5)
    label(columns[6]+8, nodes["setup-verified"].left[1]-32, "Нет")

    for node in nodes.values():
        canvas.node(node)
    for x, y, value in labels:
        canvas.text(x, y, value, backing=True)

    # The gray legend describes genuinely unavailable/deferred product steps.
    canvas.rect(40, 1595, WIDTH-80, 135, fill=GRAY, radius=14)
    canvas.text(58, 1609, "Серым: недоступно или отложено в текущем сценарии", size=TITLE_SIZE, color="#40504D")
    canvas.text(58, 1648, "Почта и фоновый мониторинг · Нативный импорт навыков и ZIP в ГигаАгенте · MCP Obsidian и Яндекс Диска", color="#485652")
    canvas.text(58, 1677, "Код работает с предоставленными файлами. Доступ к сервисам и облакам требует реальных инструментов. ○ Выбранные ветки идут параллельно.", color="#485652")
    canvas.text(58, 1706, "Ведущий агент и получатель могут быть одним агентом. Готовая папка сама по себе не означает настройку сервиса. Пунктир — возврат или ожидаемые данные.", color="#485652")
    canvas.save(out, "user-journey")
    if not main_only:
        build_context_detail(out)
    print(json.dumps({"svg": str((out / 'user-journey.svg').resolve()), "preview": str((out / 'user-journey.png').resolve()),
                      "nodes": len(nodes), "font_sizes": [TITLE_SIZE, BODY_SIZE], "dimensions": [WIDTH, canvas.height]}, ensure_ascii=False))


def execution_legend(canvas, x, y):
    for label, key in (("Код: проверки и файлы", "code"), ("Модель и пользователь", "model"), ("Инструменты сервиса", "tools")):
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
