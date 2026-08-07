"""Rendering a frozen report card to PDF.

Renders from `ReportCard.content` -- the snapshot taken at issue -- never from
live data. That is the whole reason the snapshot exists: a correction made in
week 14 must not silently change the document a family received in week 12.
"""

from __future__ import annotations

import io

from django.core.files.base import ContentFile
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=16,
                                spaceAfter=2 * mm),
        "meta": ParagraphStyle("m", parent=base["Normal"], fontSize=9,
                               textColor=colors.HexColor("#555555")),
        "h2": ParagraphStyle("h", parent=base["Heading2"], fontSize=11,
                             spaceBefore=4 * mm, spaceAfter=2 * mm),
        "small": ParagraphStyle("s", parent=base["Normal"], fontSize=8,
                                textColor=colors.HexColor("#777777")),
    }


def render_pdf(report) -> bytes:
    content = report.content
    styles = _styles()
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Report card - {content['student']['name']}",
    )

    story = [
        Paragraph("Report Card", styles["title"]),
        Paragraph(
            f"{content['student']['name']} &nbsp;&middot;&nbsp; "
            f"{content['student']['admission_number']} &nbsp;&middot;&nbsp; "
            f"{content['class_group']} &nbsp;&middot;&nbsp; {content['term']}",
            styles["meta"],
        ),
        Spacer(1, 6 * mm),
    ]

    rows = [["Subject", "Assessment", "Mark", "%", "Grade"]]
    spans = []
    for subject in content.get("subjects", []):
        start = len(rows)
        entries = subject.get("assessments") or [None]
        for index, entry in enumerate(entries):
            if entry is None:
                rows.append([subject["subject"], "-", "-", "-", "-"])
                continue
            grade = entry.get("grade") or {}
            mark = ("Absent" if entry["is_absent"]
                    else f"{entry['raw_score']} / {entry['max_score']}")
            rows.append([
                subject["subject"] if index == 0 else "",
                entry["title"],
                mark,
                "-" if entry["percentage"] is None else f"{entry['percentage']:.1f}",
                grade.get("label", "-"),
            ])
        if len(entries) > 1:
            spans.append(("SPAN", (0, start), (0, len(rows) - 1)))

        average = subject.get("average")
        rows.append(["", "Subject average", "",
                     "-" if average is None else f"{average:.1f}", ""])

    table = Table(rows, colWidths=[38 * mm, 58 * mm, 30 * mm, 20 * mm, 20 * mm],
                  repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 1), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f9fafb")]),
        *spans,
    ]))
    story.append(table)

    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        f"Revision {report.revision} &nbsp;&middot;&nbsp; issued "
        f"{content.get('generated_at', '')} &nbsp;&middot;&nbsp; "
        f"reference {report.id}",
        styles["small"],
    ))
    if report.supersedes_id:
        story.append(Paragraph(
            "This document replaces an earlier revision. The earlier revision "
            "remains on record.",
            styles["small"],
        ))

    doc.build(story)
    return buffer.getvalue()


def attach_pdf(report):
    """Render and store the PDF against the report card."""
    pdf = render_pdf(report)
    name = f"{report.student_id}-{report.term_id}-r{report.revision}.pdf"
    report.document.save(name, ContentFile(pdf), save=True)
    return report
