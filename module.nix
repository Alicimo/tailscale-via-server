{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services."tailscale-via-server";
  inherit (lib)
    mkEnableOption
    mkIf
    mkOption
    mkDefault
    types
    ;

  effectiveUser = if cfg.user != null then cfg.user else config.system.primaryUser;
  proxyUrl = "http://127.0.0.1:${toString cfg.localPort}";
  label = "com.tailscale-via-server";
  home = config.system.primaryUserHome;
  proxyScript = pkgs.writeText "tailscale-connect-proxy.py" (
    builtins.readFile ./tailscale_connect_proxy.py
  );
  remoteProxyCommand =
    "exec python3 - --port ${toString cfg.remotePort} --parent-pid \"$PPID\""
    + lib.optionalString cfg.verbose " --verbose";
  remoteCommand = "exec /bin/sh -c ${lib.escapeShellArg remoteProxyCommand}";
  tunnel = pkgs.writeShellScript "tailscale-via-server-tunnel" ''
    set -u

    # Refuse to run if this agent is ever loaded outside the configured session.
    if [ "$(/usr/bin/id -un)" != ${lib.escapeShellArg effectiveUser} ]; then
      exit 0
    fi

    umask 077
    /bin/mkdir -p ${lib.escapeShellArg "${home}/Library/Logs"}
    : > ${lib.escapeShellArg "${home}/Library/Logs/tailscale-via-server.out.log"}
    : > ${lib.escapeShellArg "${home}/Library/Logs/tailscale-via-server.err.log"}

    /usr/bin/ssh \
      -o BatchMode=yes \
      -o StrictHostKeyChecking=yes \
      -o ConnectTimeout=10 \
      -o ConnectionAttempts=1 \
      -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o RequestTTY=no \
      -T \
      -L 127.0.0.1:${toString cfg.localPort}:127.0.0.1:${toString cfg.remotePort} \
      ${lib.escapeShellArg cfg.sshHost} \
      ${lib.escapeShellArg remoteCommand} \
      < ${proxyScript} \
      >> ${lib.escapeShellArg "${home}/Library/Logs/tailscale-via-server.out.log"} \
      2>> ${lib.escapeShellArg "${home}/Library/Logs/tailscale-via-server.err.log"}

    # A clean remote exit is still a tunnel outage, so ask launchd to retry.
    exit 1
  '';
  cli = pkgs.writeShellApplication {
    name = "tailscale-via-server";
    runtimeInputs = [
      cfg.tailscalePackage
      pkgs.curl
    ];
    text = ''
      set -u
      proxy_url=${lib.escapeShellArg proxyUrl}
      service_domain="gui/$(id -u)/${label}"

      proxy_health() {
        curl --fail --silent --show-error --head \
          --proxy "$proxy_url" --noproxy "" \
          --connect-timeout 5 --max-time 10 \
          'https://controlplane.tailscale.com/key?v=142' >/dev/null
      }

      case ''${1:-status} in
        status)
          status=0
          if launchctl print "$service_domain" >/dev/null 2>&1; then
            printf 'LaunchAgent: loaded (%s)\n' "$service_domain"
          else
            printf 'LaunchAgent: not loaded (%s)\n' "$service_domain"
            status=1
          fi
          if proxy_health; then
            printf 'Server proxy: healthy (%s)\n' "$proxy_url"
          else
            printf 'Server proxy: unavailable (%s)\n' "$proxy_url"
            status=1
          fi
          if tailscale status; then
            :
          else
            printf 'Tailscale: status command failed\n'
            status=1
          fi
          exit "$status"
          ;;
        login)
          if ! proxy_health; then
            printf 'Server proxy is unavailable; refusing to start Tailscale login.\n' >&2
            exit 1
          fi
          export HTTP_PROXY="$proxy_url" HTTPS_PROXY="$proxy_url"
          export http_proxy="$proxy_url" https_proxy="$proxy_url"
          unset NO_PROXY no_proxy ALL_PROXY all_proxy
          shift
          exec tailscale login --accept-dns=false "$@"
          ;;
        restart)
          exec launchctl kickstart -k "$service_domain"
          ;;
        *)
          printf 'Usage: tailscale-via-server {status|login|restart} [tailscale login args...]\n' >&2
          exit 2
          ;;
      esac
    '';
  };
in
{
  options.services."tailscale-via-server" = {
    enable = mkEnableOption "the Tailscale connection through an external server";

    user = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "macOS user whose login session owns the server tunnel";
    };

    sshHost = mkOption {
      type = types.str;
      default = "server";
      description = "SSH host alias for the external server";
    };

    localPort = mkOption {
      type = types.ints.between 1 65535;
      default = 39082;
      description = "Loopback port used by tailscaled and the CLI";
    };

    remotePort = mkOption {
      type = types.ints.between 1 65535;
      default = 39081;
      description = "Loopback port used by the streamed proxy on the external server";
    };

    verbose = mkOption {
      type = types.bool;
      default = false;
      description = "Log successful proxy CONNECT requests as well as denials and errors";
    };

    tailscalePackage = mkOption {
      type = types.package;
      default = pkgs.tailscale;
      defaultText = lib.literalExpression "pkgs.tailscale";
      description = "The official Tailscale package used for tailscaled and tailscale";
    };
  };

  config = mkIf cfg.enable {
    system.primaryUser = mkIf (cfg.user != null) (mkDefault cfg.user);

    assertions = [
      {
        assertion = effectiveUser != null && effectiveUser != "";
        message = "services.\"tailscale-via-server\" requires `user` or `system.primaryUser`.";
      }
      {
        assertion = cfg.user == null || config.system.primaryUser == cfg.user;
        message = "services.\"tailscale-via-server\".user must match system.primaryUser.";
      }
    ];

    services.tailscale.enable = true;
    services.tailscale.package = cfg.tailscalePackage;

    environment.systemPackages = [ cli ];

    launchd.daemons.tailscaled.serviceConfig.EnvironmentVariables = {
      HTTP_PROXY = proxyUrl;
      HTTPS_PROXY = proxyUrl;
      http_proxy = proxyUrl;
      https_proxy = proxyUrl;
      NO_PROXY = "";
      no_proxy = "";
      ALL_PROXY = "";
      all_proxy = "";
    };

    launchd.user.agents."tailscale-via-server" = {
      command = tunnel;
      serviceConfig = {
        Label = label;
        RunAtLoad = true;
        KeepAlive = {
          SuccessfulExit = false;
        };
        ThrottleInterval = 30;
        EnvironmentVariables = {
          HOME = home;
          USER = effectiveUser;
        };
      };
    };
  };
}
