{
  description = "Awning control script for Bond Bridge";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        pythonEnv = pkgs.python3.withPackages (ps: with ps; [
          requests
          python-dotenv
          rich
          pvlib
          pandas
          pytz
          zeroconf
        ]);

        awning = pkgs.writeScriptBin "awning" ''
          #!${pkgs.bash}/bin/bash
          export PYTHONPATH="${./.}:$PYTHONPATH"
          exec ${pythonEnv}/bin/python3 ${./awning.py} "$@"
        '';

        awning-automation = pkgs.writeScriptBin "awning-automation" ''
          #!${pkgs.bash}/bin/bash
          export PYTHONPATH="${./.}:$PYTHONPATH"
          exec ${pythonEnv}/bin/python3 ${./awning_automation.py} "$@"
        '';
      in
      {
        packages = {
          default = awning;
          awning = awning;
          automation = awning-automation;
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [
            pythonEnv
            pkgs.jq  # For compatibility with existing workflow if needed
          ];

          shellHook = ''
            echo "Awning development environment"
            echo "Python: $(python3 --version)"
            echo ""
            echo "Run 'python3 awning.py --help' to test the script"
          '';
        };

        apps = {
          default = {
            type = "app";
            program = "${awning}/bin/awning";
          };
          automation = {
            type = "app";
            program = "${awning-automation}/bin/awning-automation";
          };
        };
      }
    );
}
