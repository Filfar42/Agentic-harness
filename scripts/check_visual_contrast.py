"""Measure the committed UI palette, including composite state backgrounds.

Run ``python scripts/check_visual_contrast.py --write-report`` from any folder.
The checker uses only the standard library, reads the actual CSS tokens, and
exits nonzero when a measured pair fails. It is a palette regression check,
not an automated assertion of complete WCAG conformance or rendered CSS audit.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RGB = tuple[float, float, float]


@dataclass(frozen=True)
class Color:
    """An sRGB color with optional alpha; channel values are in [0, 1]."""

    rgb: RGB
    alpha: float = 1.0

    def over(self, background: Color) -> Color:
        """Composite over an opaque background in CSS's sRGB color space."""
        if background.alpha != 1:
            raise ValueError("The backing color must be opaque")
        values = tuple(
            foreground * self.alpha + backing * (1 - self.alpha)
            for foreground, backing in zip(self.rgb, background.rgb, strict=True)
        )
        return Color((values[0], values[1], values[2]))

    def opacity(self, value: float) -> Color:
        """Apply element opacity to the foreground for an explicit test case."""
        if not 0 <= value <= 1:
            raise ValueError("Opacity outside [0, 1]")
        return Color(self.rgb, self.alpha * value)

    @property
    def hex(self) -> str:
        """Return rounded display values; comparisons retain full precision."""
        return "#" + "".join(f"{round(channel * 255):02x}" for channel in self.rgb)


def luminance(color: Color) -> float:
    """Return relative luminance using the WCAG sRGB transfer function."""
    channels = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in color.rgb
    ]
    return sum(
        channel * weight for channel, weight in zip(channels, (0.2126, 0.7152, 0.0722), strict=True)
    )


def contrast(foreground: Color, background: Color) -> float:
    """Return contrast after compositing any foreground transparency."""
    light, dark = sorted(
        (luminance(foreground.over(background)), luminance(background)), reverse=True
    )
    return (light + 0.05) / (dark + 0.05)


def clean_css(path: Path) -> str:
    """Read CSS and remove comments before matching declarations."""
    return re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)


def block(css: str, selector: str) -> str:
    """Return the final block for an exact selector in these non-nested files."""
    matches = re.findall(r"(?:^|})\s*" + re.escape(selector) + r"\s*\{([^{}]*)}", css)
    if not matches:
        raise ValueError(f"CSS selector not found: {selector}")
    return matches[-1]


def declaration(css: str, selector: str, property_name: str) -> str:
    """Read one required declaration, failing rather than inventing a default."""
    bodies = re.findall(r"(?:^|})\s*" + re.escape(selector) + r"\s*\{([^{}]*)}", css)
    matches = [
        value
        for body in bodies
        for value in re.findall(r"(?:^|;)\s*" + re.escape(property_name) + r"\s*:\s*([^;]+)", body)
    ]
    if not matches:
        raise ValueError(f"Missing {property_name} in {selector}")
    return matches[-1].strip()


class Palette:
    """Resolve this project's hexadecimal, rgba and alias color tokens."""

    def __init__(self, css: str, selector: str = ":root") -> None:
        root = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block(css, ":root")))
        if selector != ":root":
            root.update(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block(css, selector)))
        self.tokens = root

    def color(self, value: str, seen: frozenset[str] = frozenset()) -> Color:
        """Resolve a supported color; reject unknown expressions and cycles."""
        value = value.strip()
        alias = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if alias:
            value = alias.group(1)
        if value.startswith("--"):
            if value in seen:
                raise ValueError(f"Cyclic color token: {value}")
            return self.color(self.tokens[value], seen | {value})
        if re.fullmatch(r"#[\da-fA-F]{6}", value):
            return Color(tuple(int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)))
        rgba = re.fullmatch(r"rgba?\(([^)]+)\)", value)
        if rgba:
            values = [float(part.strip()) for part in rgba.group(1).split(",")]
            if len(values) in (3, 4) and all(0 <= v <= 255 for v in values[:3]):
                alpha = values[3] if len(values) == 4 else 1
                if 0 <= alpha <= 1:
                    return Color((values[0] / 255, values[1] / 255, values[2] / 255), alpha)
        raise ValueError(f"Unsupported CSS color: {value}")


@dataclass(frozen=True)
class Measurement:
    """A deliberately selected semantic foreground/background pair."""

    theme: str
    group: str
    label: str
    foreground: Color
    background: Color
    minimum: float = 4.5

    @property
    def ratio(self) -> float:
        return contrast(self.foreground, self.background)

    @property
    def passed(self) -> bool:
        return self.ratio >= self.minimum


def desktop_cases(css: str, theme: str) -> list[Measurement]:
    """Cover text, status tints, primary actions, control boundaries and focus."""
    palette = Palette(css, ":root" if theme == "Desktop light" else 'html[data-theme="dark"]')
    results: list[Measurement] = []

    def add(
        group: str, label: str, foreground: Color, background: Color, minimum: float = 4.5
    ) -> None:
        results.append(Measurement(theme, group, label, foreground, background, minimum))

    surfaces = (
        "bg",
        "bg-subtle",
        "surface",
        "surface-alt",
        "hover-bg",
        "selected-bg",
        "accent-soft",
        "code-bg",
    )
    for foreground in ("text", "text-muted", "text-faint", "accent", "ok", "warn", "err"):
        for background in surfaces:
            add(
                f"Testo --{foreground}",
                f"--{foreground} / --{background}",
                palette.color(f"--{foreground}"),
                palette.color(f"--{background}"),
            )
    for background in ("accent", "accent-hover"):
        add(
            "Azioni primarie",
            f"--on-accent / --{background}",
            palette.color("--on-accent"),
            palette.color(f"--{background}"),
        )
    for background in ("text-muted", "err"):
        add(
            "Interruzione",
            f"--surface / --{background}",
            palette.color("--surface"),
            palette.color(f"--{background}"),
        )
    for background in ("bg", "bg-subtle", "surface", "surface-alt"):
        add(
            "Contorni controlli",
            f"--control-border / --{background}",
            palette.color("--control-border"),
            palette.color(f"--{background}"),
            3,
        )
    for background in surfaces:
        add(
            "Focus e selezione",
            f"--focus-ring / --{background}",
            palette.color("--focus-ring"),
            palette.color(f"--{background}"),
            3,
        )
    for state in ("ok", "warn"):
        background = palette.color(f"--tint-{state}").over(palette.color("--surface"))
        for foreground in (state, "text-faint"):
            add(
                "Piano: tinte di stato",
                f"--{foreground} / --tint-{state} su --surface",
                palette.color(f"--{foreground}"),
                background,
            )
    error_background = palette.color("--err").opacity(0.08).over(palette.color("--surface"))
    add("Errore: tinta", "--err / --err 8% su --surface", palette.color("--err"), error_background)
    opacity = float(declaration(css, ".hint-inline", "opacity"))
    for background in ("surface", "bg-subtle"):
        add(
            "Note inline con opacità",
            f"--text-faint (opacity {opacity:g}) / --{background}",
            palette.color("--text-faint").opacity(opacity),
            palette.color(f"--{background}"),
        )
    opacity = float(declaration(css, "details.drawer > summary .chev", "opacity"))
    for foreground in ("text-muted", "accent"):
        add(
            "Chevron espansione",
            f"--{foreground} (opacity {opacity:g}) / --surface-alt",
            palette.color(f"--{foreground}").opacity(opacity),
            palette.color("--surface-alt"),
            3,
        )
    return results


def mobile_cases(css: str) -> list[Measurement]:
    """Include pressed actions, messages, code tint and activity opacity."""
    palette = Palette(css)
    results: list[Measurement] = []

    def add(
        group: str, label: str, foreground: Color, background: Color, minimum: float = 4.5
    ) -> None:
        results.append(Measurement("Mobile dark", group, label, foreground, background, minimum))

    for foreground in ("testo", "testo-dim"):
        for background in ("bg", "panel", "panel-hover", "utente"):
            add(
                f"Testo --{foreground}",
                f"--{foreground} / --{background}",
                palette.color(f"--{foreground}"),
                palette.color(f"--{background}"),
            )
    for background in ("accento", "accento-hover", "accento-active"):
        add(
            "Azioni primarie",
            f"--su-accento / --{background}",
            palette.color("--su-accento"),
            palette.color(f"--{background}"),
        )
    for name, background in (
        ("Normale", palette.color("--stop")),
        ("Premuto", palette.color(declaration(css, ".stop:active", "background"))),
    ):
        add("Interruzione", name, palette.color("--su-stop"), background)
    for background in ("bg", "panel", "panel-hover"):
        add(
            "Contorni controlli",
            f"--bordo-control / --{background}",
            palette.color("--bordo-control"),
            palette.color(f"--{background}"),
            3,
        )
        add(
            "Focus e selezione",
            f"--accento / --{background}",
            palette.color("--accento"),
            palette.color(f"--{background}"),
            3,
        )
    add(
        "Errore: banner",
        "--errore / --errore-bg",
        palette.color("--errore"),
        palette.color("--errore-bg"),
    )
    opacity = float(declaration(css, ".passo", "opacity"))
    for foreground in ("testo-dim", "errore"):
        add(
            "Passi con opacità",
            f"--{foreground} (opacity {opacity:g}) / --bg",
            palette.color(f"--{foreground}").opacity(opacity),
            palette.color("--bg"),
        )
    for background in ("panel", "utente"):
        tint = palette.color(declaration(css, ".msg code", "background")).over(
            palette.color(f"--{background}")
        )
        add(
            "Codice inline",
            f"--testo / .msg code su --{background}",
            palette.color("--testo"),
            tint,
        )
    for selector, foreground in (
        (".msg.pending", "testo"),
        (".msg.pending.risposta-data", "testo-dim"),
    ):
        tint = palette.color(declaration(css, selector, "background")).over(palette.color("--bg"))
        add(
            "Domande e risposte",
            f"--{foreground} / {selector}",
            palette.color(f"--{foreground}"),
            tint,
        )
    # The badge's entire box fades: composite both text and fill at its minimum.
    frames = re.search(r"@keyframes respira\s*\{(.*?)\n}", css, flags=re.S)
    if frames is None:
        raise ValueError("Missing badge animation keyframes")
    opacity = min(float(value) for value in re.findall(r"opacity:\s*([.\d]+)", frames.group(1)))
    backdrop = palette.color("--panel")
    badge = palette.color(declaration(css, ".badge-running", "background"))
    add(
        "Badge attività",
        f"--ok / badge, opacità minima {opacity:g}",
        palette.color("--ok").opacity(opacity).over(backdrop),
        badge.opacity(opacity).over(backdrop),
    )
    return results


def report(measurements: list[Measurement]) -> str:
    """Write a compact worst-case summary plus a reproducible complete matrix."""
    failures = [case for case in measurements if not case.passed]
    lines = [
        "# Verifica del contrasto — Graphite / Mineral",
        "",
        f"**{len(measurements) - len(failures)}/{len(measurements)} combinazioni conformi alla soglia misurata.**",
        "",
        "Il controllo legge i token correnti di `web/style.css` e `web_mobile/style.css`. "
        "Misura testo normale, metadati, stati hover/selezionati, azioni premute, tinte "
        "composite, opacità esplicite e indicatori di focus. Non assume l'esenzione per testo grande: "
        "usa 4.5:1 per tutto il testo, anche quello più piccolo. La soglia è quella di "
        "[WCAG 2.1, contrasto minimo](https://www.w3.org/WAI/WCAG21/Understanding/contrast-minimum.html).",
        "",
        "Per contorni funzionali, icone significative e focus usa 3:1 rispetto allo sfondo "
        "adiacente, secondo [WCAG 2.1, contrasto non testuale](https://www.w3.org/WAI/WCAG21/Understanding/non-text-contrast.html). "
        "I separatori decorativi `--border`, i loghi e i controlli disabilitati non sono trattati "
        "come contorni funzionali. Gli stati selezionati usano l'accento come indicatore.",
        "",
        "È una verifica della palette e delle combinazioni elencate, **non una certificazione "
        "WCAG completa**. Non dimostra da sola semantica accessibile, navigazione da tastiera, "
        "assenza di sovrapposizioni, comportamento dei componenti nativi, contrasto di contenuti "
        "caricati dall'utente o contrasto durante ogni fotogramma delle transizioni. "
        "Il testo delle aree scorrevoli non viene piu' sfumato da maschere CSS.",
        "",
        "## Riproduzione",
        "",
        "```powershell",
        ".venv\\Scripts\\python.exe scripts/check_visual_contrast.py --write-report",
        "```",
        "",
        "Nessuna dipendenza esterna. L'esito restituisce codice 1 se una coppia fallisce. "
        "Le tinte vengono composte in sRGB prima del calcolo della luminanza; il confronto "
        "usa valori non arrotondati. I rapporti e gli esadecimali mostrati sono arrotondati.",
        "",
        "## Minimo per famiglia",
        "",
        "| Tema | Famiglia | Peggiore combinazione | Rapporto | Soglia | Esito |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    groups = dict.fromkeys((case.theme, case.group) for case in measurements)
    for theme, group in groups:
        worst = min(
            (case for case in measurements if case.theme == theme and case.group == group),
            key=lambda case: case.ratio,
        )
        lines.append(
            f"| {theme} | {group} | {worst.label} | {worst.ratio:.2f}:1 | {worst.minimum:g}:1 | {'PASS' if worst.passed else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "## Matrice completa",
            "",
            "<details>",
            "<summary>Mostra tutte le combinazioni misurate</summary>",
            "",
            "| Tema | Combinazione | Testo/segno effettivo | Sfondo effettivo | Rapporto | Soglia | Esito |",
            "| --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for case in measurements:
        lines.append(
            f"| {case.theme} | {case.label} | {case.foreground.over(case.background).hex} | {case.background.hex} | {case.ratio:.2f}:1 | {case.minimum:g}:1 | {'PASS' if case.passed else 'FAIL'} |"
        )
    lines.extend(["", "</details>", ""])
    return "\n".join(lines)


def main() -> int:
    """Measure current sources, optionally regenerate the report, and set status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-report", action="store_true", help="Regenerate docs/brand/CONTRAST.md"
    )
    args = parser.parse_args()
    desktop = clean_css(ROOT / "web/style.css")
    mobile = clean_css(ROOT / "web_mobile/style.css")
    measurements = (
        desktop_cases(desktop, "Desktop light")
        + desktop_cases(desktop, "Desktop dark")
        + mobile_cases(mobile)
    )
    if args.write_report:
        path = ROOT / "docs/brand/CONTRAST.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report(measurements), encoding="utf-8")
    failures = [case for case in measurements if not case.passed]
    for case in failures:
        print(f"FAIL {case.theme}: {case.label}: {case.ratio:.3f}:1 < {case.minimum:g}:1")
    print(f"Contrast checks: {len(measurements) - len(failures)}/{len(measurements)} passed")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
