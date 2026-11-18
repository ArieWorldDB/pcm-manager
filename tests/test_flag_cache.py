def test_nation_flag_is_fetched_once_and_served_from_local_cache(
    app, client, monkeypatch, tmp_path,
):
    from webapp.web import routes

    calls = []
    image_data = b'\x89PNG\r\n\x1a\nflag-image'

    class Response:
        status = 200
        headers = {'Content-Type': 'image/png'}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return image_data

    def fetch(request, timeout):
        calls.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(routes.urllib.request, 'urlopen', fetch)
    app.config['FLAG_CACHE_DIR'] = str(tmp_path / 'flags')

    first = client.get('/flags/au')
    second = client.get('/flags/au')

    assert first.status_code == second.status_code == 200
    assert first.data == second.data == image_data
    assert first.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    assert len(calls) == 1
    assert (tmp_path / 'flags' / 'au.png').read_bytes() == image_data


def test_nation_flag_rejects_unknown_codes(client, monkeypatch):
    from webapp.web import routes

    monkeypatch.setattr(
        routes.urllib.request, 'urlopen',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('unexpected fetch')),
    )

    assert client.get('/flags/unknown').status_code == 404
