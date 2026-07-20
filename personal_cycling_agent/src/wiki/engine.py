"""
Wiki Engine — core operations for creating, reading, and updating wiki pages.

The wiki lives as a directory of markdown files under the vault. This
module provides the filesystem-level operations; the LLM handles content
generation through the ingest module.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.config import vault_path

logger = logging.getLogger(__name__)

# Wiki directory layout
WIKI_DIR = "wiki"
ENTITIES_DIR = "entities"
CONCEPTS_DIR = "concepts"
SOURCES_DIR = "sources"
ANALYSES_DIR = "analyses"
SYNTHESSES_DIR = "syntheses"

INDEX_FILE = "index.md"
LOG_FILE = "log.md"


def wiki_path() -> Path:
    """Return the root wiki directory path."""
    return vault_path() / WIKI_DIR


def ensure_wiki() -> Path:
    """Create the wiki directory structure if it doesn't exist."""
    root = wiki_path()
    root.mkdir(parents=True, exist_ok=True)
    for subdir in (ENTITIES_DIR, CONCEPTS_DIR, SOURCES_DIR, ANALYSES_DIR, SYNTHESSES_DIR):
        (root / subdir).mkdir(parents=True, exist_ok=True)
    # Ensure index.md exists
    index = root / INDEX_FILE
    if not index.exists():
        index.write_text(_initial_index(), encoding="utf-8")
    # Ensure log.md exists
    log = root / LOG_FILE
    if not log.exists():
        log.write_text("# Wiki Log\n\n", encoding="utf-8")
    # Seed default wiki content if the wiki is fresh
    _seed_default_pages(root)
    return root


def _seed_default_pages(root: Path) -> None:
    """Seed default wiki pages for training, recovery, and knee issues.
    
    Only creates pages that don't already exist — safe to call repeatedly.
    """
    _default_pages = [
        {
            "directory": CONCEPTS_DIR,
            "slug": "training-zones",
            "content": (
                "---\n"
                "title: Training Zones\n"
                "tags: [training, power, zones, ftp]\n"
                "created: 2024-01-01\n"
                "---\n\n"
                "# Training Zones\n\n"
                "## Overview\n"
                "Training zones define intensity ranges used to structure workouts.\n"
                "They are typically based on Functional Threshold Power (FTP) or\n"
                "maximum heart rate.\n\n"
                "## Power Zones (Coggan 8-zone model)\n\n"
                "| Zone | Name | % FTP | Description |\n"
                "|------|------|-------|-------------|\n"
                "| 1 | Active Recovery | < 55% | Very easy, promotes recovery |\n"
                "| 2 | Endurance | 56-75% | Aerobic base, conversational pace |\n"
                "| 3 | Tempo | 76-90% | Moderately hard, sustainable ~1h |\n"
                "| 4 | Lactate Threshold | 91-105% | Sustainable ~1h, \"comfortably hard\" |\n"
                "| 5 | VO2 Max | 106-120% | 3-8 min efforts, very hard |\n"
                "| 6 | Anaerobic | 121-150% | 10-30 sec efforts |\n"
                "| 7 | Neuromuscular | > 150% | < 10 sec maximal efforts |\n\n"
                "## Heart Rate Zones\n\n"
                "HR zones are derived from maximum heart rate and resting heart rate\n"
                "(Karvonen formula) or from lactate threshold testing.\n\n"
                "## Practical Use\n\n"
                "- **Zone 2** forms the base of most training plans (80% of volume)\n"
                "- **Zone 3** is used for tempo rides and race-pace practice\n"
                "- **Zone 4-5** intervals build threshold and VO2 max\n"
                "- **Zone 6-7** sprints develop neuromuscular power\n"
            ),
        },
        {
            "directory": CONCEPTS_DIR,
            "slug": "recovery-protocols",
            "content": (
                "---\n"
                "title: Recovery Protocols\n"
                "tags: [recovery, wellness, hrv, sleep]\n"
                "created: 2024-01-01\n"
                "---\n\n"
                "# Recovery Protocols\n\n"
                "## Overview\n"
                "Recovery is when adaptation happens. Training creates stress;\n"
                "recovery builds the physiological response.\n\n"
                "## Key Recovery Metrics\n\n"
                "- **HRV (Heart Rate Variability)**: Higher HRV generally indicates\n"
                "  better autonomic recovery. Track morning RMSSD trends.\n"
                "- **Resting Heart Rate**: Elevated RHR can signal incomplete recovery.\n"
                "- **Sleep Quality**: Duration and quality directly impact recovery.\n"
                "- **TSB (Training Stress Balance)**: CTL - ATL. Positive = fresh,\n"
                "  negative = fatigued.\n\n"
                "## Recovery Strategies\n\n"
                "### Immediate (0-2 hours post-ride)\n"
                "- Light spinning or walking to flush metabolites\n"
                "- Nutrition: carbs + protein within 30 minutes\n"
                "- Hydration: replace 150% of fluid lost\n\n"
                "### Same Day\n"
                "- Compression garments (moderate evidence)\n"
                "- Cold water immersion for next-day performance\n"
                "- Foam rolling for perceived recovery\n\n"
                "### Overnight\n"
                "- 7-9 hours of quality sleep\n"
                "- Cool, dark sleeping environment\n"
                "- Consistent sleep schedule\n\n"
                "### Multi-Day\n"
                "- Active recovery rides (Zone 1, 30-60 min)\n"
                "- Deload weeks: reduce volume by 40-60%\n"
                "- Monitor TSB and HRV trends\n"
            ),
        },
        {
            "directory": CONCEPTS_DIR,
            "slug": "knee-health",
            "content": (
                "---\n"
                "title: Knee Health for Cyclists\n"
                "tags: [knee, injury, biomechanics, recovery]\n"
                "created: 2024-01-01\n"
                "---\n\n"
                "# Knee Health for Cyclists\n\n"
                "## Common Cycling Knee Issues\n\n"
                "### Patellofemoral Pain Syndrome (PFPS)\n"
                "- Pain around or behind the kneecap\n"
                "- Often caused by tracking issues, muscle imbalances\n"
                "- Aggravated by high cadence with poor form or low cadence with high torque\n\n"
                "### Iliotibial Band Syndrome (ITBS)\n"
                "- Lateral knee pain, typically at ~30 degrees of flexion\n"
                "- Often related to saddle height, cleat position, or hip weakness\n"
                "- Pain usually appears after a certain duration or distance\n\n"
                "### Patellar Tendonitis\n"
                "- Pain at the bottom of the kneecap\n"
                "- Overuse injury from high-intensity efforts\n"
                "- Responds to eccentric strengthening\n\n"
                "## Prevention Strategies\n\n"
                "### Bike Fit\n"
                "- **Saddle height**: Leg nearly straight at bottom of pedal stroke\n"
                "- **Cleat position**: Center of pressure under ball of foot\n"
                "- **Q-factor**: Match to hip width\n"
                "- **Fore-aft saddle position**: Knee over pedal spindle at 3 o'clock\n\n"
                "### Strength Training\n"
                "- Quadriceps: squats, lunges, step-ups\n"
                "- Hamstrings: deadlifts, Nordic curls\n"
                "- Glutes: hip thrusts, clamshells\n"
                "- Hip abductors: side-lying raises, banded walks\n\n"
                "### Training Modifications\n"
                "- Gradual increase in volume and intensity\n"
                "- Cadence work: practice 80-100 rpm range\n"
                "- Avoid sudden spikes in training load\n"
                "- Include rest days and recovery weeks\n\n"
                "## Rehabilitation Principles\n\n"
                "1. **Reduce pain**: Modify training, not necessarily stop completely\n"
                "2. **Restore mobility**: Soft tissue work, stretching\n"
                "3. **Build strength**: Eccentric and isometric exercises\n"
                "4. **Return to sport**: Gradual progression with monitoring\n"
            ),
        },
        {
            "directory": CONCEPTS_DIR,
            "slug": "training-load-management",
            "content": (
                "---\n"
                "title: Training Load Management\n"
                "tags: [training, tss, ctl, atl, tsb, load]\n"
                "created: 2024-01-01\n"
                "---\n\n"
                "# Training Load Management\n\n"
                "## Key Metrics\n\n"
                "- **TSS (Training Stress Score)**: Quantifies the load of a single session\n"
                "- **CTL (Chronic Training Load)**: 42-day exponential moving average of TSS\n"
                "- **ATL (Acute Training Load)**: 7-day exponential moving average of TSS\n"
                "- **TSB (Training Stress Balance)**: CTL - ATL (form/fatigue)\n\n"
                "## CTL Targets\n\n"
                "| Level | CTL Range | Description |\n"
                "|-------|-----------|-------------|\n"
                "| Beginner | 30-50 | Building base |\n"
                "| Intermediate | 50-80 | Developing fitness |\n"
                "| Advanced | 80-100 | High training volume |\n"
                "| Elite | 100+ | Professional-level training |\n\n"
                "## Planning Principles\n\n"
                "### Build Phase\n"
                "- Increase CTL by 2-5 points per week\n"
                "- Keep ATL close to CTL (TSB near zero)\n"
                "- Avoid CTL increases > 10% week-over-week\n\n"
                "### Peak Phase\n"
                "- Reduce ATL while maintaining CTL (TSB becomes positive)\n"
                "- Typical taper: 10-14 days, reduce volume 30-50%\n"
                "- Maintain some intensity to keep neuromuscular sharpness\n\n"
                "### Recovery Phase\n"
                "- Allow ATL to catch down after hard blocks\n"
                "- Active recovery rides in Zone 1\n"
                "- Monitor HRV and RHR for recovery status\n\n"
                "## Warning Signs\n"
                "- TSB below -20 for extended periods: risk of overtraining\n"
                "- CTL dropping > 10 points in a week: detraining begins\n"
                "- ATL spiking > 20% above CTL: acute overload risk\n"
            ),
        },
    ]

    for page in _default_pages:
        target = root / page["directory"] / f'{page["slug"]}.md'
        if not target.exists():
            target.write_text(page["content"], encoding="utf-8")
            logger.info(f"Seeded wiki page: {page['directory']}/{page['slug']}.md")

def _initial_index() -> str:
    return """# Wiki Index

*Auto-generated. Updated on every ingest.*

## Sources
_No sources ingested yet._

## Entities
_No entities yet._

## Concepts
_No concepts yet._

## Analyses
_No analyses yet._

## Syntheses
_No syntheses yet._
"""


# ── Page I/O ───────────────────────────────────────────────────────────

def _slug(title: str) -> str:
    """Convert a title to a filesystem-safe slug."""
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug or "untitled"


def _page_dir(page_type: str) -> str:
    """Map a page type to its directory."""
    mapping = {
        "entity": ENTITIES_DIR,
        "concept": CONCEPTS_DIR,
        "source": SOURCES_DIR,
        "analysis": ANALYSES_DIR,
        "synthesis": SYNTHESSES_DIR,
    }
    return mapping.get(page_type, "concepts")


def read_page(directory: str, slug: str) -> str | None:
    """Read a wiki page by directory and slug. Returns None if not found."""
    path = wiki_path() / directory / f"{slug}.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_page(directory: str, slug: str, content: str) -> Path:
    """Write a wiki page. Returns the path written."""
    root = ensure_wiki()
    page_dir = root / directory
    page_dir.mkdir(parents=True, exist_ok=True)
    path = page_dir / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    logger.info(f"Wrote wiki page: {path.relative_to(vault_path())}")
    return path


def list_pages(directory: str) -> list[dict[str, Any]]:
    """List all pages in a directory with frontmatter metadata."""
    root = wiki_path() / directory
    if not root.exists():
        return []
    pages = []
    for md_file in sorted(root.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        meta = _parse_frontmatter(content)
        pages.append({
            "slug": md_file.stem,
            "path": str(md_file.relative_to(vault_path())),
            "type": meta.get("type", ""),
            "title": meta.get("title", md_file.stem),
            "created": meta.get("created", ""),
            "updated": meta.get("updated", ""),
            "category": meta.get("category", ""),
        })
    return pages


def all_pages() -> list[dict[str, Any]]:
    """List all wiki pages across all directories."""
    result = []
    for directory in (ENTITIES_DIR, CONCEPTS_DIR, SOURCES_DIR, ANALYSES_DIR, SYNTHESSES_DIR):
        result.extend(list_pages(directory))
    return result


# ── Frontmatter ────────────────────────────────────────────────────────

def _parse_frontmatter(content: str) -> dict[str, str]:
    """Parse YAML-like frontmatter from a markdown page."""
    meta: dict[str, str] = {}
    if not content.startswith("---"):
        return meta
    try:
        end = content.index("---", 3)
        block = content[3:end].strip()
        for line in block.split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
    except ValueError:
        pass
    return meta


def _build_frontmatter(meta: dict[str, str]) -> str:
    """Build a frontmatter block from metadata."""
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


# ── Search ─────────────────────────────────────────────────────────────

def search_pages(query: str, directories: list[str] | None = None) -> list[dict[str, Any]]:
    """
    Search wiki pages for a query string.
    Returns matching pages ranked by relevance (simple keyword matching).
    """
    if directories is None:
        directories = [ENTITIES_DIR, CONCEPTS_DIR, SOURCES_DIR, ANALYSES_DIR, SYNTHESSES_DIR]

    query_lower = query.lower()
    terms = query_lower.split()
    results: list[dict[str, Any]] = []

    for directory in directories:
        for page_info in list_pages(directory):
            slug = page_info["slug"]
            content = read_page(directory, slug)
            if content is None:
                continue

            content_lower = content.lower()
            score = 0

            # Title match is worth more
            title = page_info.get("title", "").lower()
            for term in terms:
                if term in title:
                    score += 10
                if term in content_lower:
                    score += 1

            if score > 0:
                results.append({
                    **page_info,
                    "directory": directory,
                    "score": score,
                    "snippet": _extract_snippet(content, terms),
                })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def _extract_snippet(content: str, terms: list[str], length: int = 120) -> str:
    """Extract a snippet of content around the first matching term."""
    content_lower = content.lower()
    for term in terms:
        idx = content_lower.find(term)
        if idx >= 0:
            start = max(0, idx - 40)
            end = min(len(content), idx + length)
            snippet = content[start:end].strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(content):
                snippet = snippet + "..."
            return snippet
    # Fallback: first line of content
    first_line = content.split("\n", 1)[0]
    if len(first_line) > length:
        return first_line[:length] + "..."
    return first_line


# ── Stats ──────────────────────────────────────────────────────────────

def wiki_stats() -> dict[str, Any]:
    """Return statistics about the wiki."""
    stats = {
        "entities": 0,
        "concepts": 0,
        "sources": 0,
        "analyses": 0,
        "syntheses": 0,
        "total_pages": 0,
        "total_size_bytes": 0,
    }
    root = wiki_path()
    if not root.exists():
        return stats

    for directory, key in [
        (ENTITIES_DIR, "entities"),
        (CONCEPTS_DIR, "concepts"),
        (SOURCES_DIR, "sources"),
        (ANALYSES_DIR, "analyses"),
        (SYNTHESSES_DIR, "syntheses"),
    ]:
        dir_path = root / directory
        if dir_path.exists():
            count = len(list(dir_path.glob("*.md")))
            stats[key] = count
            stats["total_pages"] += count
            for f in dir_path.glob("*.md"):
                stats["total_size_bytes"] += f.stat().st_size

    return stats
# ── Default wiki seeding ───────────────────────────────────────────────

_DEFAULT_PAGES: list[dict[str, str]] = [
    {
        "title": "Training",
        "category": "training",
        "body": (
            "## Training Principles\n\n"
            "Effective cycling training is built on a foundation of volume at low intensity, "
            "punctuated by targeted high-intensity work. The polarized training model — roughly "
            "80% of time in Zone 2 and below, 20% at or above threshold — is the most evidence-backed "
            "approach for building aerobic capacity without excessive fatigue.\n\n"
            "Zone 2 training develops mitochondrial density, capillary networks, and fat oxidation. "
            "It feels easy enough to hold a conversation, but requires discipline to keep heart rate "
            "in range. Threshold intervals (3–10 minutes at FTP) raise the ceiling, while shorter "
            "high-intensity efforts (30s–3min) improve VO2 max and neuromuscular power.\n\n"
            "Periodize your training: base phases emphasize Zone 2 volume, build phases add threshold "
            "work, and peak phases include race-specific intensity. Always follow hard weeks with "
            "reduced volume to allow adaptation."
        ),
    },
    {
        "title": "Recovery",
        "category": "recovery",
        "body": (
            "## Recovery Strategies\n\n"
            "Recovery is where adaptation happens. Training creates the stimulus; rest builds the "
            "capacity. Without adequate recovery, training load accumulates as fatigue rather than "
            "fitness, leading to plateaus and overtraining.\n\n"
            "Sleep is the single most important recovery tool. Aim for 7–9 hours per night, with "
            "consistent sleep and wake times. Nutrition supports recovery: consume carbohydrates "
            "and protein within 2 hours post-ride (roughly 3:1 carb-to-protein ratio). Hydration "
            "should replace 150% of fluid lost during exercise.\n\n"
            "Active recovery — light spinning at 50–60% FTP for 30–60 minutes — increases blood "
            "flow and clears metabolic waste without adding stress. Schedule at least one full rest "
            "day per week, and reduce volume by 40–60% during recovery weeks every 3–4 weeks."
        ),
    },
    {
        "title": "Knee Issues",
        "category": "injury-prevention",
        "body": (
            "## Common Cycling Knee Issues\n\n"
            "Cyclist's knee typically presents as patellofemoral pain (around or behind the kneecap) "
            "or IT band syndrome (lateral knee pain). Both are overuse injuries caused by repetitive "
            "loading combined with biomechanical imbalances. Pain that is sharp, localized, and "
            "worsens with pedaling is a signal to investigate, not push through.\n\n"
            "Prevention starts with bike fit. The cleat position should allow natural foot alignment; "
            "the saddle height should produce a slight knee bend (25–35°) at the bottom of the "
            "stroke. A saddle that is too high causes overreaching and lateral knee tracking; too "
            "low increases joint compression. The fore-aft saddle position affects knee-over-pedal "
            "alignment at the midpoint of the stroke.\n\n"
            "Strengthen the hips and glutes — weak abductors and external rotators are the most common "
            "root cause of cycling knee pain. Clamshells, lateral band walks, and single-leg squats "
            "are effective. If pain persists beyond two weeks of reduced load and corrected fit, "
            "consult a sports physiotherapist."
        ),
    },
    {
        "title": "Nutrition",
        "category": "nutrition",
        "body": (
            "## Cycling Nutrition Basics\n\n"
            "Fueling for cycling is about matching carbohydrate intake to effort duration and "
            "intensity. Rides under 60 minutes rarely require in-ride fueling beyond water. "
            "For sessions of 60–90 minutes, aim for 30–45g of carbohydrates per hour. Endurance "
            "rides over 90 minutes benefit from 60–90g per hour, using multiple transportable "
            "carbohydrates (glucose + fructose in a 2:1 ratio) to maximize absorption.\n\n"
            "Pre-ride nutrition sets the stage. Eat a carbohydrate-rich meal 2–3 hours before "
            "longer rides, or a smaller snack 30–60 minutes before shorter sessions. Post-ride, "
            "prioritize protein (20–30g) and carbohydrates to replenish glycogen and repair muscle. "
            "Daily energy availability — calories consumed minus exercise expenditure — should stay "
            "above 30 kcal/kg of fat-free mass to maintain health and performance.\n\n"
            "Hydration strategy: weigh yourself before and after rides to estimate sweat rate. "
            "For every kilogram lost, drink 1.5 liters of fluid over the next few hours. Add "
            "electrolytes (sodium, potassium, magnesium) for rides over 90 minutes or in hot "
            "conditions."
        ),
    },
    {
        "title": "Mental Training",
        "category": "mental-performance",
        "body": (
            "## Mental Aspects of Cycling Performance\n\n"
            "Cycling is as much a mental sport as a physical one. The ability to manage discomfort, "
            "maintain focus, and execute under fatigue separates good riders from great ones. "
            "Mental skills are trainable: they improve with deliberate practice just like "
            "physiological fitness.\n\n"
            "Pacing discipline is the most impactful mental skill. Start efforts conservatively — "
            "the urge to go hard early is universal, but negative splits (second half faster than "
            "the first) are the hallmark of smart racing. Use power targets or heart rate zones "
            "as anchors when motivation wanes. Break long efforts into manageable segments: focus "
            "on the next 10 minutes, not the remaining 2 hours.\n\n"
            "Build resilience through exposure. Regularly include sessions that push your comfort "
            "zone — threshold intervals, long climbs, fast group rides — so that race-day discomfort "
            "feels familiar. Visualization helps: mentally rehearse key moments (attack, sprint, "
            "descent) before you face them. Journaling post-ride to capture what went well and what "
            "to improve creates a feedback loop that accelerates learning."
        ),
    },
]


def seed_default_wiki() -> list[str]:
    """Seed default wiki pages for all users on first run.

    Creates default concept pages only if they do not already exist.
    Returns a list of slugs that were created.
    """
    from src.wiki.index import rebuild_index

    root = ensure_wiki()
    created: list[str] = []

    for page_spec in _DEFAULT_PAGES:
        slug = _slug(page_spec["title"])
        existing = read_page(CONCEPTS_DIR, slug)
        if existing is not None:
            logger.debug(f"Wiki page already exists, skipping: {slug}")
            continue

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        meta = {
            "title": page_spec["title"],
            "type": "concept",
            "category": page_spec["category"],
            "created": now,
            "updated": now,
        }
        content = _build_frontmatter(meta) + "\n\n" + page_spec["body"]
        write_page(CONCEPTS_DIR, slug, content)
        created.append(slug)

    if created:
        rebuild_index()
        logger.info(f"Seeded {len(created)} default wiki page(s): {', '.join(created)}")

    return created