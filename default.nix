{ pkgs }:
let
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
python.pkgs.buildPythonApplication {
  pname = "tubefin";
  version = "1.7.3";
  pyproject = true;

  src = ./.;

  postPatch = ''
    substituteInPlace src/tubefin/mpv_player.py \
      --replace-fail '"libEGL.so.1"' '"${pkgs.libglvnd}/lib/libEGL.so.1"' \
      --replace-fail '"libGL.so.1"' '"${pkgs.libglvnd}/lib/libGL.so.1"' \
      --replace-fail '"libgtk-4.so.1"' '"${mediaGtk}/lib/libgtk-4.so.1"'
  '';

  build-system = [ python.pkgs.setuptools ];
  dependencies = [
    python.pkgs.pygobject3
    ytDlp
    pythonMpv
    python.pkgs.pynacl
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
    mediaGtk
    pkgs.webkitgtk_6_0
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
      --set GDK_MEDIA_ONLY_HDR 1
      --set GDK_DEBUG color-mgmt,force-offload
      --prefix GI_TYPELIB_PATH : ${mediaGtk}/lib/girepository-1.0
      --prefix LD_LIBRARY_PATH : ${mediaGtk}/lib
      --prefix PATH : ${
        pkgs.lib.makeBinPath [
          ytDlp
          pkgs.deno
          pkgs.ffmpeg
          pkgs.libsecret
        ]
      }
      --prefix LD_LIBRARY_PATH : ${
        pkgs.lib.makeLibraryPath [
          pkgs.mpv
          pkgs.libglvnd
        ]
      }
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
    install -Dm644 data/com.neg-zero.Tubefin.desktop \
      $out/share/applications/com.neg-zero.Tubefin.desktop
    install -Dm644 data/com.neg-zero.Tubefin.metainfo.xml \
      $out/share/metainfo/com.neg-zero.Tubefin.metainfo.xml
    install -Dm644 data/com.neg-zero.Tubefin.svg \
      $out/share/icons/hicolor/scalable/apps/com.neg-zero.Tubefin.svg
  '';

  meta = {
    description = "Native YouTube and Jellyfin client for GNOME";
    homepage = "https://github.com/doromiert/tubefin";
    license = pkgs.lib.licenses.gpl3Plus;
    mainProgram = "tubefin";
    platforms = pkgs.lib.platforms.linux;
  };
}
