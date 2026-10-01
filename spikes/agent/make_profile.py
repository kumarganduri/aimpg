"""Hardened replay profile (spike): Legwork's read rules + proxy-only network."""
import os, sys
from pathlib import Path

home = str(Path.home().resolve())
workdir, cfgdir, port = (str(Path(sys.argv[1]).resolve()), str(Path(sys.argv[2]).resolve()), sys.argv[3])
tool_dirs = [Path(home, ".local/share/claude"), Path(home, ".local/bin"), Path(home, ".local/share/uv/python")]
MACH = ["com.apple.system.opendirectoryd.libinfo", "com.apple.system.opendirectoryd.membership",
        "com.apple.system.notification_center", "com.apple.system.logger", "com.apple.logd",
        "com.apple.diagnosticd", "com.apple.SystemConfiguration.configd",
        "com.apple.SystemConfiguration.DNSConfiguration", "com.apple.dnssd.service", "com.apple.trustd",
        "com.apple.trustd.agent", "com.apple.cfprefsd.daemon", "com.apple.cfprefsd.agent"]
lines = ["(version 1)", "(deny default)", "(allow process-fork)", "(allow process-exec)", "(allow sysctl-read)",
         "(allow mach-lookup " + " ".join(f'(global-name "{m}")' for m in MACH) + ")", "(allow iokit-open)",
         "(allow signal (target same-sandbox))",
         f'(allow file-read* (require-not (subpath "{home}")))',
         f'(allow file-read* file-write* (subpath "{workdir}"))',
         f'(allow file-read* file-write* (subpath "{cfgdir}"))',
         '(allow file-write-data (literal "/dev/null") (literal "/dev/zero"))',
         '(allow file-write* (regex #"^/private/var/folders/[^/]+/[^/]+/T/"))',
         "(allow system-socket)",
         f'(allow network-outbound (remote ip "localhost:{port}"))']
for d in tool_dirs:
    lines.append(f'(allow file-read* (subpath "{d.resolve()}"))')
anc = set()
for target in [Path(workdir), Path(cfgdir), *[d.resolve() for d in tool_dirs]]:
    p = target
    while str(p).startswith(home) and str(p) != home:
        p = p.parent
        anc.add(str(p))
for a in sorted(anc):
    lines.append(f'(allow file-read-metadata (literal "{a}"))')
print("\n".join(lines))
