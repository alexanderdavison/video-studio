#!/usr/bin/env python3
"""Build the Test 3 prompt: Test 2's prompt with exactly two edits, both required to
expose the duration choice and nothing else.

  1. a descriptive section explaining that the candidate id now also fixes the length
  2. the output-schema example id (b_downbeat no longer exists)

No advice about which length to choose, and no duplication of rule 2's existing
statement that hold_a is always acceptable.
"""
import sys

SRC = "/root/vs3/pkg/prompt_test2.md"
SEC = "/root/vs3/pkg/prompt_test3_neutral.md"
OUT = "/root/vs3/pkg/prompt_test3.md"

base = open(SRC).read()
section = open(SEC).read().rstrip()
base = base.replace("      \"selection\": \"b_downbeat\",", "      \"selection\": \"b_action\",")
assert "b_downbeat" not in base, "an old candidate id survived in the prompt"
assert "## Hard rules" in base
base = base.replace("## Hard rules", section + "\n\n## Hard rules", 1)
base = base.replace("You are the **editorial selector** for a DJ set edit.",
                    "You are the **editorial selector** for a DJ set edit.", 1)
open(OUT, "w").write(base)
print("wrote", OUT, len(base), "chars")
print("contains duration section:", "THE DURATION CHOICE" in base)
print("contains prescriptive advice:", any(
    s in base for s in ("the honest choice", "Choosing `hold_a` remains", "Judge the length")))
sys.exit(0)
