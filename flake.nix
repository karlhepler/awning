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

        # What the app needs at runtime. Keep in step with requirements.txt.
        runtimePackages = ps: with ps; [
          requests
          python-dotenv
          rich
          pvlib
          pandas
          tenacity
          pillow
        ];
        pythonEnv = pkgs.python3.withPackages runtimePackages;
        # The dev shell adds pytest; it no longer ships inside the app closure.
        devPythonEnv = pkgs.python3.withPackages (ps: runtimePackages ps ++ [ ps.pytest ]);

        awning = pkgs.writeScriptBin "awning" ''
          #!${pkgs.bash}/bin/bash
          export PYTHONPATH="${./.}''${PYTHONPATH:+:$PYTHONPATH}"
          exec ${pythonEnv}/bin/python3 ${./awning.py} "$@"
        '';

        awning-automation = pkgs.writeScriptBin "awning-automation" ''
          #!${pkgs.bash}/bin/bash
          export PYTHONPATH="${./.}''${PYTHONPATH:+:$PYTHONPATH}"
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
            devPythonEnv
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
