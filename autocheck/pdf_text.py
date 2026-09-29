"""Geometry shared by checks that must distinguish review furniture from text."""

from statistics import median


def get_review_number_bboxes(page, blocks=None):
    """Return bboxes of aligned, increasing review line-number columns.

    A number must sit outside the main text's horizontal extent and belong to
    a vertical sequence of at least three consecutive numbers. A short sequence
    must also use a smaller font than the adjacent body text; otherwise a table's
    first column can be mistaken for review numbers. Same-size numbers require
    a dense, regular ruler running down most of a page's outer side margin.
    Both edges are clustered because line numbers may be left- or right-aligned.
    """
    if blocks is None:
        blocks = page.get_text("dict", flags=0)["blocks"]
    lines = [("".join(span["text"] for span in line["spans"]).strip(),
              tuple(line["bbox"]), max((span["size"] for span in line["spans"]), default=0))
             for block in blocks for line in block.get("lines", [])]
    body = [(bbox, size) for text, bbox, size in lines
            if len(text) >= 20 and not text.isdigit() and bbox[2] - bbox[0] >= 80]
    if not body:
        return set()
    # Trim occasional protruding lines without letting indented lines or titles
    # pull the estimated left edge deeply into the body.
    lefts = sorted(bbox[0] for bbox, _ in body)
    rights = sorted(bbox[2] for bbox, _ in body)
    left = lefts[int((len(lefts) - 1) * 0.1)]
    right = rights[int((len(rights) - 1) * 0.9)]
    rect = page.rect
    body_size = median(size for _, size in body)
    candidates = [(int(text), bbox, size) for text, bbox, size in lines
                  if text.isascii() and text.isdigit() and len(text) <= 6
                  and rect.x0 <= bbox[0] <= bbox[2] <= rect.x1
                  and (bbox[2] < left - 4 or bbox[0] > right + 4)]
    ignored = set()

    def accept_run(run):
        if len(run) < 3:
            return
        # Use text at the same height when available, so smaller-font table
        # cells are compared to their own row rather than to larger paragraphs.
        def is_small(bbox, size):
            peers = [other_size for text, box, other_size in lines
                     if not text.isdigit() and text
                     and abs((box[1] + box[3] - bbox[1] - bbox[3]) / 2) < body_size * 0.7]
            return size < 0.9 * (median(peers) if peers else body_size)

        # An occasional small caption/URL on the same row must not invalidate
        # an otherwise established ruler. Short runs still need every row.
        small_count = sum(is_small(bbox, size) for _, bbox, size in run)
        if small_count >= max(3, len(run) * 0.8):
            ignored.update(bbox for _, bbox, _ in run)
            return
        # ICML uses body-size numbers on a fixed ruler, including blank areas.
        # Requiring the outer page margin, a long span and regular short spacing
        # distinguishes that ruler from an ordinary numbered table column.
        if len(run) < 20 or run[-1][1][1] - run[0][1][1] < rect.height * 0.5:
            return
        if not all(bbox[2] < rect.x0 + rect.width * 0.1
                   or bbox[0] > rect.x1 - rect.width * 0.1 for _, bbox, _ in run):
            return
        gaps = [second[1][1] - first[1][1] for first, second in zip(run, run[1:])]
        spacing = median(gaps)
        if spacing <= body_size * 2 and all(abs(gap - spacing) < 2 for gap in gaps):
            ignored.update(bbox for _, bbox, _ in run)

    for edge in (0, 2):
        columns = []
        for item in sorted(candidates, key=lambda item: item[1][edge]):
            if columns and item[1][edge] - columns[-1][0][1][edge] <= 2:
                columns[-1].append(item)
            else:
                columns.append([item])
        for column in columns:
            run = []
            for number, bbox, size in sorted(column, key=lambda item: item[1][1]):
                if run and (number != run[-1][0] + 1 or bbox[1] <= run[-1][1][1] + 2):
                    accept_run(run)
                    run = []
                run.append((number, bbox, size))
            accept_run(run)
    return ignored
