# Review notes

What came back from agents, what I accepted, what I rejected, and what I changed. Kept as I went
rather than written up afterwards. The brief asks whether I am a critical reader of agent output,
and this is the honest record of that rather than a claim about it.

## 1. Research memo on legal document hierarchy

**Agent.** Background researcher, told to work from primary sources and to flag anything it could
not verify rather than guessing.

**What it did well.** It went to the normative Akoma Ntoso XSD rather than to summaries of it, and
came back with the exact list of 27 hierarchy elements, the fact that the standard is a vocabulary
with no fixed ordering, and the branch or leaf content model. That last one independently confirms
the leaf only text decision I had already made on other grounds, which is the most useful thing in
the memo. It also found a Court of Appeal judgment, Al Mana Lifestyle Trading v United Fidelity
Insurance [2023] EWCA Civ 61, where the parties had to add their own bracketed numbers to a clause
because sentences carry no native identifier. That is much better evidence for not modelling
sentences than the reasoning I had.

**What I rejected.** It stated that RM6116 reaches four decimal levels, citing `9.1.3.2`. I checked
and there are zero four level numbered lines in the pack. The claim is wrong and the cited example
does not exist. Corrected in the memo with the check that disproves it.

**What I corrected in my own work because of it.** My spec said there was exactly one roman numeral
item, which was true of the Core Terms and false of the pack, where there are 82. That was my error,
not the agent's, and the memo caught it.

**What checking it turned up.** Following up its claim that the pack calls `1.3.8` a Clause led to
a real inconsistency in the document worth designing around. See the memo's verification section
and the named resolver case in `SPEC.md`.

