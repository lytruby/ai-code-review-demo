from src.locate import first_added_line, locate_issue, locate_text, patch_after_lines

PATCH = """@@ -10,4 +10,5 @@ def handler(request):
     user = request.user
-    if user:
+    if user is not None:
+        audit(user)
         return render(user)
     return None
"""


def test_patch_after_lines_numbers_head_side():
    assert patch_after_lines(PATCH) == [
        (10, "    user = request.user"),
        (11, "    if user is not None:"),
        (12, "        audit(user)"),
        (13, "        return render(user)"),
        (14, "    return None"),
    ]


def test_first_added_line():
    assert first_added_line(PATCH) == 11
    assert first_added_line("") is None


def test_locate_text_ignores_indentation_and_partial_edges():
    lines = patch_after_lines(PATCH)
    assert locate_text("if user is not None:\n  audit(user)", lines) == (11, 12)
    assert locate_text("audit(", lines) == (12, 12)
    assert locate_text("missing()", lines) is None


def test_locate_issue_falls_back_to_head_file_then_first_added_line(tmp_path):
    (tmp_path / "app.py").write_text("import os\n\ndef audit(user):\n    log(user)\n")
    assert locate_issue("app.py", ["def audit(user):\n    log(user)"], PATCH, tmp_path) == (3, 4)
    assert locate_issue("app.py", ["nowhere"], PATCH, tmp_path) == (11, 11)
    assert locate_issue("app.py", ["nowhere"], "", None) is None
