import sys

from tubefin.application import TubeFinApplication


def main() -> int:
    app = TubeFinApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
