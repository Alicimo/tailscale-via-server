# Tailscale via server on macOS

This flake provides a nix-darwin module that runs the normal, system-wide
`tailscaled` through a restricted HTTP proxy on a configurable external
server. It does not create a second userspace Tailscale node and does not
replace the official `tailscale` CLI.

## Architecture

The module starts the existing nix-darwin `services.tailscale` LaunchDaemon and
sets its `HTTP_PROXY` and `HTTPS_PROXY` (including lowercase variants) to a
fixed loopback URL. A per-user LaunchAgent starts at login and keeps one
foreground SSH session open. That session:

1. forwards `127.0.0.1:39082` to `127.0.0.1:39081` on the server; and
2. runs the proxy by streaming this repository's Python file to remote Python
   over that same SSH session.

No remote file or remote service is installed. The tunnel uses macOS's native
SSH client so Apple-specific settings such as `UseKeychain` remain supported.
SSH uses BatchMode, strict host key checking, a connection timeout,
forward-failure checking, no TTY, and server-alive checks. launchd restarts the
session with throttled retries. The proxy monitors its SSH parent process so a
dropped session cannot leave an orphan holding the remote port.

Only HTTPS CONNECT requests to port 443 whose host is exactly, or is a
subdomain of, `tailscale.com` or `tailscale.io` are allowed. Tailscale
coordination and DERP HTTPS traffic can therefore use the server. Direct peer
traffic remains direct between the normal Tailscale node and its peers; it is
not sent through this HTTP proxy.

This is not a DNS forwarder. The external server resolves the `tailscale.com`
and `tailscale.io` endpoint names contained in CONNECT requests, but MagicDNS
and the Mac's ordinary upstream DNS queries keep their existing
Tailscale/macOS behavior.

If the SSH session or proxy is absent, the fixed proxy URL has nothing
listening and Tailscale control-plane/DERP HTTP(S) traffic fails closed rather
than falling back to a direct HTTP connection. The loopback listener is
intentionally unauthenticated and trusted only by the local machine.

Only denials and errors are logged by default. Set `verbose = true` to also log
startup and successful ALLOW lines. The local log files are truncated whenever
launchd starts a new SSH attempt, preventing repeated outage messages from
accumulating indefinitely.

## Module API and setup

The flake exports `darwinModules.default`. Add its input and module to the
consumer's flake:

```nix
inputs.tailscale-via-server.url = "github:Alicimo/tailscale-via-server";
inputs.tailscale-via-server.inputs.nixpkgs.follows = "nixpkgs";
inputs.tailscale-via-server.inputs.nix-darwin.follows = "nix-darwin";

modules = [
  inputs.tailscale-via-server.darwinModules.default
  ({ ... }: {
    system.primaryUser = "alice";
    services."tailscale-via-server" = {
      enable = true;
      user = "alice"; # optional when system.primaryUser is set
      sshHost = "your-server";
      localPort = 39082;
      remotePort = 39081;
      verbose = false;
    };
  })
];
```

The exact namespace is `services."tailscale-via-server"`. `user` must match
`system.primaryUser`; setting `user` also supplies `system.primaryUser` when it
was not otherwise set. `sshHost`, `localPort`, `remotePort`, and `verbose` are
the remaining options. Ports are validated as integers from 1 through 65535
and may use the same number because they listen on different machines.

The two commands for initial setup are:

```sh
darwin-rebuild switch --flake .#your-hostname
tailscale-via-server login
```

The second command is a one-time login (or can be repeated when needed). It
checks the server proxy first and then invokes the real Tailscale browser
login. Existing Tailscale state remains durable and normal key expiry rules
still apply.

## Prerequisites

- macOS with nix-darwin and this flake available to the configuration.
- An existing noninteractive SSH key and SSH config for the configured server.
  The server's host key must already be in `known_hosts`; interactive host-key
  prompts are deliberately disabled.
- The SSH account on the server must have `/bin/sh`, be able to run Python 3.12,
  and make the permitted outbound HTTPS connections. No root SSH credentials
  are needed.
- The configured macOS user must be the user running the SSH configuration and
  login session.

## Operations and troubleshooting

```sh
tailscale-via-server status
tailscale-via-server restart
launchctl print gui/$(id -u)/com.tailscale-via-server
tail -f ~/Library/Logs/tailscale-via-server.out.log ~/Library/Logs/tailscale-via-server.err.log
tailscale status
```

`status` checks that the user LaunchAgent is loaded, makes a real HTTPS request
through the loopback proxy, and runs the normal `tailscale status`. `restart`
kickstarts the user tunnel agent. If the proxy is unavailable, first inspect
the log and then verify the SSH alias, host key, key agent/configuration, and
the server's Python 3.12. Port conflicts are also shown by the SSH forward's
failure and the LaunchAgent will retry.

To disable or uninstall the integration, remove the module (or set its
`enable` option to `false`) and run `darwin-rebuild switch` again. This removes
the managed LaunchAgent and the proxy environment from the generated system;
it does not delete durable Tailscale state. Key expiry is still handled by
Tailscale, so run `tailscale-via-server login` again after reauthentication is
required.
