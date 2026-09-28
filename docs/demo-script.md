# Demo Script

Start the server (see README) and open http://localhost:8010. Press **Reset demo** first if you've already run it.

1. **Safe change, auto-approved.** Sample *Add loyalty tier to customers*. Scores LOW, rollback is rehearsed and
   verified, synthetic rows insert cleanly, and policy auto-approves. Click **Deploy to production** and show the
   validation checks.
2. **Data change needing review.** Sample *Close stale NEW orders*. MEDIUM: shows rows affected, and the Dependency
   agent flags the audit trigger that will fire. Show that the requester (`ops.bot`) can't approve their own change,
   then approve as `lee.lead` (LEAD) and deploy.
3. **Destructive change with a real rollback.** Sample *Retire products.active flag*. HIGH, needs a DBA. Walk through
   the rollback plan: the before-image backup and the re-add + restore script. Approve as `dana.dba`, deploy, then
   **Roll back**, and show the "checksums match the pre-deploy state" verification.
4. **Caught before production.** Samples *Purge inactive products*, *Drop orders.total* and *Clean up customers
   table*. Each is BLOCKED: the dry run on a production clone fails (FK violation, broken view), and the Dependency
   agent shows which object breaks.
5. **Audit.** Open the **Audit log** tab: every agent step, approval, deploy and rollback, with actor and time.
   The **Schema** tab shows the live database, e.g. the `loyalty_tier` column added in step 1.
