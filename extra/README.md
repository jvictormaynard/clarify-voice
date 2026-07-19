# Bundled SoX runtime

`sox-14.4.2/` contains the Windows SoX runtime used for audio conversion in the
portable build. The application invokes `sox.exe` as a separate process.

The upstream license is preserved at:

- `sox-14.4.2/LICENSE.GPL.txt`

The former `sox.zip` copy was removed because it duplicated this directory and
was not used by the application, setup, build, or release workflows.
