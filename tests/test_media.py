from localnet.media import table_from_html, table_rows


def test_office_clipboard_table_keeps_empty_cells_and_unicode():
    original = ("<html><table><tr><th>Файл</th><th>Готовность</th></tr>"
                "<tr><td></td><td><b>Готово</b> &amp; проверено</td></tr>"
                "<tr><td>Фото</td><td></td></tr></table></html>")
    text = table_from_html(original)
    assert text == "Файл\tГотовность\n\tГотово & проверено\nФото\t"
    assert table_rows(text) == [["Файл", "Готовность"], ["", "Готово & проверено"], ["Фото", ""]]
    assert table_rows("Обычный\nтекст") == []
