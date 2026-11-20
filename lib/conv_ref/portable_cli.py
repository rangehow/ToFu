"""Offline selected-chat archive CLI (stdlib only, no authority access)."""
import argparse
import json
import sys

from .portable import PortableChatError, validate_package


def _literal(text):
    # Do not emit terminal escapes or bidirectional formatting controls.
    return ''.join(c if c in '\n\t' or c.isprintable() else ascii(c)[1:-1] for c in text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog='No import or Continue commands.')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('verify', 'list', 'read', 'attachment'):
        mode = commands.add_parser(command)
        mode.add_argument('archive')
        if command == 'read':
            mode.add_argument('chat_id')
        if command == 'attachment':
            mode.add_argument('digest', help='SHA-256 of original; writes raw bytes to stdout')
    args = parser.parse_args(argv)
    try:
        with open(args.archive, 'rb') as stream:
            package = validate_package(stream.read())
        if args.command == 'verify':
            print(f'Verified: {len(package.chats)} chats, {len(package.originals)} originals')
        elif args.command == 'list':
            for chat in package.chats:
                print(json.dumps({'source': chat.source_id, 'id': chat.conversation_id,
                                  'title': json.loads(chat.document)['title']}))
        elif args.command == 'read':
            match = next((c for c in package.chats if c.conversation_id == args.chat_id), None)
            if match is None:
                raise PortableChatError('conversation_not_in_package')
            chat = json.loads(match.document)
            print(_literal(chat['title']))
            if chat['projectLabel']:
                print('Project: ' + _literal(chat['projectLabel']))
            for message in chat['messages']:
                print(f"\n{message['role']}:\n" + _literal(message['text']))
                for digest in message['originals']:
                    print(f'Original SHA-256: {digest}')
        else:
            original = dict(package.originals).get(args.digest)
            if original is None:
                raise PortableChatError('original_not_in_package')
            if sys.stdout.isatty():
                raise PortableChatError('redirect_original_to_a_file')
            sys.stdout.buffer.write(original)
    except (PortableChatError, OSError) as error:
        print(str(error) if isinstance(error, PortableChatError) else 'archive_io_error', file=sys.stderr)
        return 1
    return 0
