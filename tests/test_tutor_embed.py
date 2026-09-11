import math

from app.tutor.embed import cosine_similarity, deserialize_embedding, serialize_embedding


def test_serialize_deserialize_round_trip():
    vec = [0.1, -0.2, 0.3, 1.0, -1.0, 0.0]
    out = deserialize_embedding(serialize_embedding(vec))
    assert len(out) == len(vec)
    for a, b in zip(vec, out):
        assert math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)


def test_cosine_similarity_identical_vectors_is_one():
    v = [1.0, 2.0, 3.0]
    assert math.isclose(cosine_similarity(v, v), 1.0, rel_tol=1e-6)


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert math.isclose(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0, abs_tol=1e-9)


def test_cosine_similarity_opposite_vectors_is_negative_one():
    assert math.isclose(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0, rel_tol=1e-6)


def test_cosine_similarity_zero_vector_does_not_crash():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
