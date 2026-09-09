# Code generated. DO NOT EDIT.
# LSP v3.17.0
# TODO: Look into use of https://pypi.org/project/ts2python/ to generate the types for https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/

"""
This file provides the python interface corresponding to the requests and notifications defined in Typescript in the language server protocol.
This file is obtained from https://github.com/predragnikolic/OLSP under the MIT License with the following terms:

MIT License

Copyright (c) 2023 Предраг Николић

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from typing import Any

from solidlsp.lsp_protocol_handler import lsp_types


class LspNotification:
    def __init__(self, send_notification: Any) -> None:
        self.send_notification = send_notification

    def did_change_workspace_folders(self, params: lsp_types.DidChangeWorkspaceFoldersParams) -> None:
        """The `workspace/didChangeWorkspaceFolders` notification is sent from the client to the server when the workspace
        folder configuration changes.
        """
        return self.send_notification("workspace/didChangeWorkspaceFolders", params)

    def cancel_work_done_progress(self, params: lsp_types.WorkDoneProgressCancelParams) -> None:
        """The `window/workDoneProgress/cancel` notification is sent from  the client to the server to cancel a progress
        initiated on the server side.
        """
        return self.send_notification("window/workDoneProgress/cancel", params)

    def did_create_files(self, params: lsp_types.CreateFilesParams) -> None:
        """The did create files notification is sent from the client to the server when
        files were created from within the client.

        @since 3.16.0
        """
        return self.send_notification("workspace/didCreateFiles", params)

    def did_rename_files(self, params: lsp_types.RenameFilesParams) -> None:
        """The did rename files notification is sent from the client to the server when
        files were renamed from within the client.

        @since 3.16.0
        """
        return self.send_notification("workspace/didRenameFiles", params)

    def did_delete_files(self, params: lsp_types.DeleteFilesParams) -> None:
        """The will delete files request is sent from the client to the server before files are actually
        deleted as long as the deletion is triggered from within the client.

        @since 3.16.0
        """
        return self.send_notification("workspace/didDeleteFiles", params)

    def did_open_notebook_document(self, params: lsp_types.DidOpenNotebookDocumentParams) -> None:
        """A notification sent when a notebook opens.

        @since 3.17.0
        """
        return self.send_notification("notebookDocument/didOpen", params)

    def did_change_notebook_document(self, params: lsp_types.DidChangeNotebookDocumentParams) -> None:
        return self.send_notification("notebookDocument/didChange", params)

    def did_save_notebook_document(self, params: lsp_types.DidSaveNotebookDocumentParams) -> None:
        """A notification sent when a notebook document is saved.

        @since 3.17.0
        """
        return self.send_notification("notebookDocument/didSave", params)

    def did_close_notebook_document(self, params: lsp_types.DidCloseNotebookDocumentParams) -> None:
        """A notification sent when a notebook closes.

        @since 3.17.0
        """
        return self.send_notification("notebookDocument/didClose", params)

    def initialized(self, params: lsp_types.InitializedParams) -> None:
        """The initialized notification is sent from the client to the
        server after the client is fully initialized and the server
        is allowed to send requests from the server to the client.
        """
        return self.send_notification("initialized", params)

    def exit(self) -> None:
        """The exit event is sent from the client to the server to
        ask the server to exit its process.
        """
        return self.send_notification("exit")

    def workspace_did_change_configuration(self, params: lsp_types.DidChangeConfigurationParams) -> None:
        """The configuration change notification is sent from the client to the server
        when the client's configuration has changed. The notification contains
        the changed configuration as defined by the language client.
        """
        return self.send_notification("workspace/didChangeConfiguration", params)

    def did_open_text_document(self, params: lsp_types.DidOpenTextDocumentParams) -> None:
        """The document open notification is sent from the client to the server to signal
        newly opened text documents. The document's truth is now managed by the client
        and the server must not try to read the document's truth using the document's
        uri. Open in this sense means it is managed by the client. It doesn't necessarily
        mean that its content is presented in an editor. An open notification must not
        be sent more than once without a corresponding close notification send before.
        This means open and close notification must be balanced and the max open count
        is one.
        """
        return self.send_notification("textDocument/didOpen", params)

    def did_change_text_document(self, params: lsp_types.DidChangeTextDocumentParams) -> None:
        """The document change notification is sent from the client to the server to signal
        changes to a text document.
        """
        return self.send_notification("textDocument/didChange", params)

    def did_close_text_document(self, params: lsp_types.DidCloseTextDocumentParams) -> None:
        """The document close notification is sent from the client to the server when
        the document got closed in the client. The document's truth now exists where
        the document's uri points to (e.g. if the document's uri is a file uri the
        truth now exists on disk). As with the open notification the close notification
        is about managing the document's content. Receiving a close notification
        doesn't mean that the document was open in an editor before. A close
        notification requires a previous open notification to be sent.
        """
        return self.send_notification("textDocument/didClose", params)

    def did_save_text_document(self, params: lsp_types.DidSaveTextDocumentParams) -> None:
        """The document save notification is sent from the client to the server when
        the document got saved in the client.
        """
        return self.send_notification("textDocument/didSave", params)

    def will_save_text_document(self, params: lsp_types.WillSaveTextDocumentParams) -> None:
        """A document will save notification is sent from the client to the server before
        the document is actually saved.
        """
        return self.send_notification("textDocument/willSave", params)

    def did_change_watched_files(self, params: lsp_types.DidChangeWatchedFilesParams) -> None:
        """The watched files notification is sent from the client to the server when
        the client detects changes to file watched by the language client.
        """
        return self.send_notification("workspace/didChangeWatchedFiles", params)

    def set_trace(self, params: lsp_types.SetTraceParams) -> None:
        return self.send_notification("$/setTrace", params)

    def cancel_request(self, params: lsp_types.CancelParams) -> None:
        return self.send_notification("$/cancelRequest", params)

    def progress(self, params: lsp_types.ProgressParams) -> None:
        return self.send_notification("$/progress", params)
