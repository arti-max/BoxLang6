import argparse
from .server import run_server


def main():
    p = argparse.ArgumentParser(prog="boxlang6.debug")
    p.add_argument("input",   metavar="FILE",  help=".box file to debug")
    p.add_argument("--arch",  default="x16",   help="architecture (default: x16)")
    p.add_argument("--port",  default=8765,    type=int, help="port (default: 8765)")
    p.add_argument("--use",   default=None,    help="system config ($use directive override)")
    p.add_argument("-D", action="append", dest="defines", default=[], metavar="NAME",
                   help="define preprocessor symbol (can be used multiple times)")
    args = p.parse_args()
    run_server(args.input, arch=args.arch, port=args.port, use=args.use, defines=args.defines)


if __name__ == "__main__":
    main()