Confirm only whether the edits faithfully apply the accepted objections and
change nothing unrelated. Compare the reviewed plan and current plan using
the plan and dispositions diffs and accepted objections with host responses.
Do not raise new ideas or reopen the original goal or other dispositions.

Use the existing reviewer output schema without adding fields. If the edits
faithfully apply the accepted objections with no unrelated changes, return no
findings and goal_satisfied: true. Otherwise put every objection in findings
and prefix its summary with [unfaithful] or [unrelated_change]. Any other tag
counts as unfaithful. A finding requires the host to ask the user for a decision;
the host cannot dismiss it. Do not implement changes. Review the exact plan
and dispositions hashes in this packet.

Reading the repository: you may read any repository file to check your answer, using the read-only means your harness provides, including read-only shell commands (for example `cat`, `type`, `Get-Content`, `ls`, `rg`, `git show`, `git log`, `git diff`). Do not modify files, run tests, install packages, or execute project code. If a read is refused or unavailable, name the file and the reason in your answer instead of guessing its contents.
