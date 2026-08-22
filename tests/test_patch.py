from __future__ import annotations

from app.github.patch import parse_patch


def test_empty_patch_is_inert():
    p = parse_patch(None)
    assert p.is_empty and not p.commentable and p.added == 0


def test_line_numbers_track_the_new_file():
    patch = """@@ -1,3 +1,4 @@
 alpha
-beta
+beta2
+gamma
 delta"""
    p = parse_patch(patch)
    assert p.commentable == {1, 2, 3, 4}
    assert p.added_lines == {2, 3}
    assert p.added == 2 and p.removed == 1
    assert p.line_content[3] == "gamma"


def test_multiple_hunks_restart_at_the_right_line():
    patch = """@@ -1,2 +1,2 @@
 one
+two
@@ -50,2 +60,3 @@
 fifty
+sixty"""
    p = parse_patch(patch)
    assert 2 in p.commentable
    assert 60 in p.commentable and 61 in p.commentable
    assert 3 not in p.commentable
    assert [h.new_start for h in p.hunks] == [1, 60]


def test_deleted_lines_are_never_anchorable():
    patch = """@@ -1,3 +1,1 @@
-gone
-also gone
 kept"""
    p = parse_patch(patch)
    assert p.commentable == {1}
    assert p.removed == 2


def test_no_newline_marker_does_not_consume_a_line_number():
    patch = """@@ -1,1 +1,2 @@
 first
+second
\\ No newline at end of file"""
    p = parse_patch(patch)
    assert p.commentable == {1, 2}


def test_preamble_lines_are_not_anchorable():
    patch = """diff --git a/x.py b/x.py
index abc..def 100644
--- a/x.py
+++ b/x.py
@@ -1,1 +1,2 @@
 first
+second"""
    p = parse_patch(patch)
    assert p.commentable == {1, 2}
    # The "+++ b/x.py" header must not be mistaken for an added line.
    assert p.added == 1


def test_positions_are_recorded_for_the_legacy_api():
    p = parse_patch("@@ -1,1 +1,2 @@\n first\n+second")
    assert p.positions[1] == 2
    assert p.positions[2] == 3


def test_truncation_is_flagged_not_silent():
    patch = "@@ -1,1 +1,50 @@\n" + "\n".join(f"+line{i}" for i in range(50))
    p = parse_patch(patch, max_lines=10)
    assert p.truncated
    assert "truncated" in p.annotated
    assert len(p.commentable) < 50


def test_nearest_anchor_snaps_only_within_the_window():
    p = parse_patch("@@ -1,1 +10,3 @@\n ctx\n+added\n+more")
    assert p.nearest_anchor(11) == 11
    assert p.nearest_anchor(13) == 11 or p.nearest_anchor(13) == 12
    assert p.nearest_anchor(999) is None
    assert p.nearest_anchor(20, max_distance=3) is None


def test_nearest_anchor_prefers_added_lines_over_context():
    p = parse_patch("@@ -1,1 +1,3 @@\n context\n+added_a\n+added_b")
    # Line 0 does not exist; 1 is context, 2 is added. Both within the window.
    assert p.nearest_anchor(1) == 1
    assert p.nearest_anchor(0) == 2
