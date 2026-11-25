{
  description = "TubeFin — a native YouTube and Jellyfin client for GNOME";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system: {
        default = import ./default.nix { pkgs = import nixpkgs { inherit system; }; };
      });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/tubefin";
          meta.description = "Run TubeFin";
        };
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          ytDlp = pkgs.python3Packages.yt-dlp.overridePythonAttrs (_old: {
            version = "2026.11.23";
            postPatch = "";
            outputs = [ "out" ];
            postBuild = "";
            postInstall = "";
            src = pkgs.fetchPypi {
              pname = "yt_dlp";
              version = "2026.11.23";
              hash = "sha256-Hbg3cqjeeFWi2rVRqU1/YXyIYp399QZTHAazabR/iKQ=";
            };
          });
          mediaGtk = pkgs.gtk4.overrideAttrs (old: {
            patches = (old.patches or [ ]) ++ [ ./gtk-media-hdr.patch ];
          });
          pythonMpv = pkgs.python3Packages.buildPythonPackage {
            pname = "python-mpv";
            version = "1.0.8";
            pyproject = true;
            src = pkgs.fetchPypi {
              pname = "python_mpv";
              version = "1.0.8";
              hash = "sha256-AX+jWdoFnIMalMQZCDSRkD5tL3yBuYQcM8GWyr9LP+M=";
            };
            build-system = [ pkgs.python3Packages.setuptools ];
            postPatch = ''
              substituteInPlace mpv.py \
                --replace-fail "ctypes.util.find_library('mpv')" \
                "'${pkgs.mpv}/lib/libmpv.so.2'"
            '';
            doCheck = false;
          };
          python = pkgs.python3.withPackages (ps: [
            ps.pygobject3
            ytDlp
            pythonMpv
            ps.pynacl
            ps.websocket-client
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              mediaGtk
              pkgs.webkitgtk_6_0
              pkgs.libadwaita
              pkgs.mpv
              pkgs.libglvnd
              pkgs.gobject-introspection
              pkgs.gst_all_1.gstreamer
              pkgs.gst_all_1.gstreamer.out
              pkgs.gst_all_1.gst-plugins-base
              pkgs.gst_all_1.gst-plugins-good
              pkgs.gst_all_1.gst-plugins-bad
              pkgs.gst_all_1.gst-plugins-ugly
              pkgs.gst_all_1.gst-libav
              ytDlp
              pkgs.deno
              pkgs.ffmpeg
              pkgs.libsecret
              pkgs.ruff
            ];

            shellHook = ''
              export GDK_MEDIA_ONLY_HDR=1
              export GDK_DEBUG=color-mgmt,force-offload
              export GI_TYPELIB_PATH=${mediaGtk}/lib/girepository-1.0:''${GI_TYPELIB_PATH:-}
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              export LD_LIBRARY_PATH=${
                pkgs.lib.makeLibraryPath [
                  mediaGtk
                  pkgs.mpv
                  pkgs.libglvnd
                ]
              }''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
              export GST_PLUGIN_SYSTEM_PATH_1_0=${
                pkgs.lib.makeSearchPath "lib/gstreamer-1.0" [
                  pkgs.gst_all_1.gst-plugins-base
                  pkgs.gst_all_1.gst-plugins-good
                  pkgs.gst_all_1.gst-plugins-bad
                  pkgs.gst_all_1.gst-plugins-ugly
                  pkgs.gst_all_1.gst-libav
                  pkgs.gst_all_1.gstreamer.out
                ]
              }
              echo "TubeFin development shell"
              echo "  Run:   python -m tubefin"
              echo "  Check: ruff check src"
            '';
          };
        }
      );

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
