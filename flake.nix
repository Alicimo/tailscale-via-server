{
  description = "Run the normal macOS Tailscale daemon through a restricted external server";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    nix-darwin.url = "github:nix-darwin/nix-darwin";
    nix-darwin.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      nix-darwin,
    }:
    let
      systems = [ "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      darwinModules.default = ./module.nix;

      checks = forAllSystems (
        system:
        let
          evaluated = nix-darwin.lib.darwinSystem {
            inherit system;
            modules = [
              self.darwinModules.default
              ({ ... }: {
                system.primaryUser = "alice";
                system.stateVersion = 7;
                services."tailscale-via-server" = {
                  enable = true;
                };
                launchd.daemons.tailscaled.serviceConfig.EnvironmentVariables.UNRELATED = "preserved";
              })
            ];
          };
          invalidUser = nix-darwin.lib.darwinSystem {
            inherit system;
            modules = [
              self.darwinModules.default
              ({ ... }: {
                system.primaryUser = "alice";
                system.stateVersion = 7;
                services."tailscale-via-server" = {
                  enable = true;
                  user = "bob";
                };
              })
            ];
          };
          invalidUserResult = builtins.tryEval invalidUser.config.system.build.toplevel.drvPath;
          disabled = nix-darwin.lib.darwinSystem {
            inherit system;
            modules = [
              self.darwinModules.default
              ({ ... }: {
                system.primaryUser = "alice";
                system.stateVersion = 7;
              })
            ];
          };
          pkgs = import nixpkgs { inherit system; };
        in
        {
          proxy-tests =
            pkgs.runCommand "tailscale-via-server-proxy-tests"
              {
                nativeBuildInputs = [ pkgs.python312 ];
              }
              ''
                cp ${./tailscale_connect_proxy.py} tailscale_connect_proxy.py
                cp ${./tests/test_proxy.py} test_proxy.py
                python3 -m unittest -v test_proxy.py
                touch $out
              '';

          module-system =
            assert evaluated.config.services.tailscale.enable;
            assert
              evaluated.config.launchd.daemons.tailscaled.serviceConfig.EnvironmentVariables.HTTP_PROXY
              == "http://127.0.0.1:39082";
            assert
              evaluated.config.launchd.daemons.tailscaled.serviceConfig.EnvironmentVariables.UNRELATED
              == "preserved";
            assert
              !evaluated.config.launchd.user.agents."tailscale-via-server".serviceConfig.KeepAlive.SuccessfulExit;
            evaluated.config.system.build.toplevel;

          module-invariants =
            assert !invalidUserResult.success;
            assert !disabled.config.services.tailscale.enable;
            assert !(disabled.config.launchd.user.agents ? "tailscale-via-server");
            pkgs.runCommand "tailscale-via-server-module-invariants" { } ''
              touch $out
            '';
        }
      );
    };
}
