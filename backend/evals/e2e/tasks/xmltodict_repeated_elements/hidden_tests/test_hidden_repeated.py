import xmltodict


def test_repeated_text_elements_become_a_list():
    assert xmltodict.parse("<a><b>1</b><b>2</b></a>") == {"a": {"b": ["1", "2"]}}
    assert xmltodict.parse("<a><b>1</b><b>2</b><b>3</b></a>") == {
        "a": {"b": ["1", "2", "3"]}
    }


def test_repeated_complex_elements_keep_order():
    doc = xmltodict.parse('<r><i id="1"><v>x</v></i><c/><i id="2"><v>y</v></i></r>')
    assert doc["r"]["i"] == [
        {"@id": "1", "v": "x"},
        {"@id": "2", "v": "y"},
    ]
    assert doc["r"]["c"] is None
