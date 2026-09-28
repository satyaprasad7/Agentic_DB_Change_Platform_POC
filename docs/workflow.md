# Workflow
Request -> Analyze (dry run) -> Dependency -> Risk -> Rollback (rehearsed) -> Test (synthetic data) -> Jira -> Git -> Approve -> Deploy -> Validate -> Monitor -> Audit

Dependency analysis runs before risk scoring because the blast radius feeds the score.
A failed dry run blocks the change. A failed post-deploy validation triggers an automatic rollback.
