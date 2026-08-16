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
      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python3;
          pythonMpv = python.pkgs.buildPythonPackage {
            pname = "python-mpv";
            version = "1.0.8";
            pyproject = true;
            src = pkgs.fetchPypi {
              pname = "python_mpv";
              version = "1.0.8";
              hash = "sha256-AX+jWdoFnIMalMQZCDSRkD5tL3yBuYQcM8GWyr9LP+M=";
            };
            build-system = [ python.pkgs.setuptools ];
            postPatch = ''
              substituteInPlace mpv.py \
                --replace-fail "ctypes.util.find_library('mpv')" \
                "'${pkgs.mpv}/lib/libmpv.so.2'"
            '';
            doCheck = false;
          };
        in
        {
          default = python.pkgs.buildPythonApplication {
            pname = "tubefin";
            version = "1.1.1";
            pyproject = true;

            src = ./.;

            postPatch = ''
              substituteInPlace src/tubefin/mpv_player.py \
                --replace-fail '"libEGL.so.1"' '"${pkgs.libglvnd}/lib/libEGL.so.1"' \
                --replace-fail '"libGL.so.1"' '"${pkgs.libglvnd}/lib/libGL.so.1"' \
                --replace-fail '"libgtk-4.so.1"' '"${pkgs.gtk4}/lib/libgtk-4.so.1"'
            '';

            build-system = [ python.pkgs.setuptools ];
            dependencies = [
              python.pkgs.pygobject3
              pythonMpv
              python.pkgs.websocket-client
            ];

            # Release/package builds must not launch the test harness. Tests remain
            # available explicitly from the development shell.
            doCheck = false;

            checkPhase = ''
              runHook preCheck
              python -m unittest discover -s tests -v
              runHook postCheck
            '';

            nativeBuildInputs = [
              pkgs.wrapGAppsHook4
              pkgs.gobject-introspection
            ];

            buildInputs = [
              pkgs.gtk4
              pkgs.libadwaita
              pkgs.mpv
              pkgs.libglvnd
              pkgs.gst_all_1.gstreamer
              pkgs.gst_all_1.gstreamer.out
              pkgs.gst_all_1.gst-plugins-base
              pkgs.gst_all_1.gst-plugins-good
              pkgs.gst_all_1.gst-plugins-bad
              pkgs.gst_all_1.gst-plugins-ugly
              pkgs.gst_all_1.gst-libav
            ];

            preFixup = ''
              gappsWrapperArgs+=(
                --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.yt-dlp pkgs.ffmpeg pkgs.libsecret ]}
                --prefix LD_LIBRARY_PATH : ${pkgs.lib.makeLibraryPath [ pkgs.mpv pkgs.libglvnd ]}
                --set-default GSK_RENDERER gl
                --prefix GST_PLUGIN_SYSTEM_PATH_1_0 : ${
                  pkgs.lib.makeSearchPath "lib/gstreamer-1.0" [
                    pkgs.gst_all_1.gst-plugins-base
                    pkgs.gst_all_1.gst-plugins-good
                    pkgs.gst_all_1.gst-plugins-bad
                    pkgs.gst_all_1.gst-plugins-ugly
                    pkgs.gst_all_1.gst-libav
                    pkgs.gst_all_1.gstreamer.out
                  ]
                }
              )
            '';

            postInstall = ''
              install -Dm644 data/io.github.doromiert.TubeFin.desktop \
                $out/share/applications/io.github.doromiert.TubeFin.desktop
              install -Dm644 data/io.github.doromiert.TubeFin.metainfo.xml \
                $out/share/metainfo/io.github.doromiert.TubeFin.metainfo.xml
              install -Dm644 data/io.github.doromiert.TubeFin.svg \
                $out/share/icons/hicolor/scalable/apps/io.github.doromiert.TubeFin.svg
            '';

            meta = {
              description = "Native YouTube and Jellyfin client for GNOME";
              homepage = "https://github.com/doromiert/tubefin";
              license = pkgs.lib.licenses.gpl3Plus;
              mainProgram = "tubefin";
              platforms = pkgs.lib.platforms.linux;
            };
          };
        }
      );

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
            pythonMpv
            ps.websocket-client
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.gtk4
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
              pkgs.yt-dlp
              pkgs.ffmpeg
              pkgs.libsecret
              pkgs.ruff
            ];

            shellHook = ''
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              export LD_LIBRARY_PATH=${pkgs.lib.makeLibraryPath [ pkgs.mpv pkgs.libglvnd ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
              export GSK_RENDERER=gl
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
