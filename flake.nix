{
  description = "Conservation Dashboard Python development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  };

  outputs = { nixpkgs, ... }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
          };

          python = pkgs.python312;
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.uv
              pkgs.git
            ];

            SHELL = "${pkgs.bashInteractive}/bin/bash";

            UV_PYTHON = "${python}/bin/python";
            UV_PYTHON_DOWNLOADS = "never";

            # Makes libstdc++.so.6 available to compiled Python wheels.
            LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
              (pkgs.lib.getLib pkgs.stdenv.cc.cc)
              (pkgs.lib.getLib pkgs.zlib)
            ];

            shellHook = ''
              echo "conservation-dashboard development environment"
              echo "Python: $(python --version)"
              echo "uv:     $(uv --version)"
            '';
          };
        }
      );
    };
}
