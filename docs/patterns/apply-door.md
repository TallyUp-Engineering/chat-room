# Pattern: one apply door

When a repository generates code, policy, configuration, or schemas, expose one named command that applies generated changes. All other checks should verify that this door was used rather than inventing parallel mutation paths.

This turns generation from tribal knowledge into a reviewable contract. The same principle applies beyond compilers: one publish door, one migration door, or one provider-admission door is easier to audit than several overlapping scripts.
