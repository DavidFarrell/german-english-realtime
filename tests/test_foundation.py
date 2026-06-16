import contracts, config, fakes
def test_chunk_bytes():
    assert contracts.INPUT_CHUNK_BYTES == 3200
def test_routing_complete():
    from contracts import Direction
    assert set(config.SOURCE_FOR_DIRECTION) == set(Direction)
    assert set(config.OUTPUT_FOR_DIRECTION) == set(Direction)
