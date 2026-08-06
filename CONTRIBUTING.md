# Contributing

Issues and pull requests are welcome. Keep the project generic: no company-specific repository paths, customer concepts, private provider topology, or assumptions about one Git host.

Before opening a pull request, run:

```sh
python3 -m unittest discover -s tests -p 'test_room.py'
npm test
npm run lint
```

New coordination features must preserve the advisory boundary and fail closed for unknown targets and credential-shaped content.
