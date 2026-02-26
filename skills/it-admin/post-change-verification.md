# Post-Change Verification

## Rule (non-negotiable)

After EVERY infrastructure change, verify it worked AND nothing else broke. Not optional.

## When This Fires

- After modifying configs, restarting services, changing firewall/DNS/DHCP/network settings
- After Docker operations, security hardening, or completing audit phases

## Protocol

1. **Direct check** — Read back config, check service status, confirm setting took effect
2. **Connectivity** — curl/ping/probe the service to confirm reachability
3. **Dependencies** — Check related services still function
4. **Emit VERIFICATION block** with: change applied, direct check output, connectivity result, dependency status, PASS/FAIL

## Minimum Checks

| Change | Verify with |
|---|---|
| Service restart | `launchctl list`/`docker ps` + health endpoint |
| Firewall rule | `pfctl -sr` readback + connectivity test |
| DNS change | `dig`/`nslookup` confirmation |
| TLS cert | `openssl s_client -connect host:port` |
| Config file | Read back + restart + test functionality |
| Docker deploy | `docker ps` + `docker logs --tail 20` + health |
| Network setting | `ifconfig` readback + ping gateway |

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "Restarted without errors." | No errors ≠ working. Test the service. |
| "Config looks right." | Looking right ≠ verified. Query the running service. |
| "User will report if broken." | Verify proactively. Don't wait for user complaints. |

## Hard Gate

No change COMPLETE until VERIFICATION block shows PASS. FAIL = rollback or fix first.
