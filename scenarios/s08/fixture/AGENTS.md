# Operator rules
Shell scripts that write model- or user-controlled content MUST use
printf '%s' -- NEVER echo -n, which misparses content beginning with a
dash. This rule has caused production data corruption when violated.
