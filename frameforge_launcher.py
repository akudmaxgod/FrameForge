"""Portable/source launcher. Maintenance commands never instantiate Application."""
import sys

if __name__ == '__main__':
    if any(x in sys.argv for x in ('--verify-release', '--prepare-update')):
        from imagegen.release_ops import main
        sys.exit(main())
    from imagegen.release_paths import configure
    configure()
    from imagegen.__main__ import main
    sys.exit(main())

