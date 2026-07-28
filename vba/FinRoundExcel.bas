Attribute VB_Name = "FinRoundExcel"
Option Explicit
'==============================================================================
' FinRoundExcel - the worksheet front end for FinRound.
'
' Select a table INCLUDING its heading row and label column, run
' FinRoundSelection, and a new sheet appears with the rounded table, the
' figures that had to give way marked, and a report of what was done.
'
' Also here:
'   FinRoundToRange  - round one range into another, from your own code
'   FR_ROUND         - a worksheet function returning the rounded block
'
' All the arithmetic lives in FinRound.bas; this module only moves figures
' between the sheet and that module.
'==============================================================================

Private Const FR_MARK As Long = 13434879      ' pale amber for a figure that moved
Private Const FR_TOTAL_TINT As Long = 15921906 ' pale grey for a total

'==============================================================================
' The macro
'==============================================================================

Public Sub FinRoundSelection()
    Dim src As Range
    On Error Resume Next
    Set src = Application.Selection
    On Error GoTo 0
    If src Is Nothing Then
        MsgBox "Select the table first, including its headings and labels.", _
               vbExclamation, "finround"
        Exit Sub
    End If
    If src.Rows.Count < 2 Or src.Columns.Count < 2 Then
        MsgBox "Select at least two rows and two columns.", vbExclamation, "finround"
        Exit Sub
    End If
    If src.Cells.Count > 40000 Then
        MsgBox "That selection is very large. Select just the table.", _
               vbExclamation, "finround"
        Exit Sub
    End If

    Dim answer As String, scaleBy As Double, decimals As Long
    answer = InputBox("Divide every figure by:" & vbLf & vbLf & _
                      "1 = leave as is,  1000 = thousands," & vbLf & _
                      "100000 = lakh,  10000000 = crore,  1000000 = millions", _
                      "finround - scale", "1")
    If Len(Trim$(answer)) = 0 Then Exit Sub
    If Not IsNumeric(answer) Then
        MsgBox "That is not a number.", vbExclamation, "finround"
        Exit Sub
    End If
    scaleBy = CDbl(answer)

    answer = InputBox("Decimal places to present:", "finround - decimals", "0")
    If Len(Trim$(answer)) = 0 Then Exit Sub
    If Not IsNumeric(answer) Then
        MsgBox "That is not a number.", vbExclamation, "finround"
        Exit Sub
    End If
    decimals = CLng(answer)
    If decimals < 0 Or decimals > 6 Then
        MsgBox "Use between 0 and 6 decimal places.", vbExclamation, "finround"
        Exit Sub
    End If

    Dim note As String
    note = RoundToNewSheet(src, scaleBy, 10 ^ (-decimals), decimals)
    If Len(note) > 0 Then MsgBox note, vbInformation, "finround"
End Sub

'==============================================================================
' Reading the sheet
'==============================================================================

Private Function CellNumber(cell As Range, ByRef value As Double, _
                            ByRef isBlank As Boolean) As Boolean
    Dim v As Variant, t As String
    value = 0
    isBlank = False
    v = cell.value
    If IsError(v) Then Exit Function
    If IsEmpty(v) Then
        isBlank = True
        CellNumber = True
        Exit Function
    End If
    If Not (VarType(v) = vbString) Then
        If IsNumeric(v) Then
            value = CDbl(v)
            CellNumber = True
            Exit Function
        End If
    End If
    t = Trim$(CStr(v))
    If t = "" Or t = "-" Or t = ChrW(8211) Or t = ChrW(8212) Then
        isBlank = True
        CellNumber = True
        Exit Function
    End If
    If IsNumeric(t) Then
        value = CDbl(t)
        CellNumber = True
    End If
End Function

Private Function LooksLikeLabels(src As Range, ByVal wantRow As Boolean) As Boolean
    ' A heading row or label column is one that carries no figures at all.
    Dim k As Long, n As Long, value As Double, isBlank As Boolean
    Dim cell As Range
    If wantRow Then
        n = src.Columns.Count
    Else
        n = src.Rows.Count
    End If
    For k = 2 To n
        If wantRow Then
            Set cell = src.Cells(1, k)
        Else
            Set cell = src.Cells(k, 1)
        End If
        If CellNumber(cell, value, isBlank) Then
            If Not isBlank Then Exit Function
        End If
    Next k
    LooksLikeLabels = True
End Function

'==============================================================================
' Rounding a range
'==============================================================================

' Round src (headings and labels included) onto a new sheet. Returns a short
' note for the user, or a message beginning "finround:" if nothing was done.
Public Function RoundToNewSheet(src As Range, ByVal scaleBy As Double, _
                                ByVal stepSize As Double, ByVal decimals As Long) As String
    Dim vals() As Double, blank() As Boolean, units() As Double, naive() As Double
    Dim rowLabels() As String, colLabels() As String
    Dim rowParent() As Long, colParent() As Long
    Dim rowBad() As Boolean, colBad() As Boolean
    Dim rowCand() As Boolean, colCand() As Boolean
    Dim nR As Long, nC As Long, r0 As Long, c0 As Long
    Dim i As Long, j As Long, problem As String

    problem = ReadBlock(src, vals, blank, rowLabels, colLabels, nR, nC, r0, c0)
    If Len(problem) > 0 Then
        RoundToNewSheet = "finround: " & problem
        Exit Function
    End If

    ReDim rowCand(0 To nR - 1)
    ReDim colCand(0 To nC - 1)
    FRDetect vals, nR, nC, rowLabels, colLabels, (c0 = 2), (r0 = 2), _
             rowCand, colCand, 0, rowParent, colParent, rowBad, colBad

    If Not FRRound(vals, nR, nC, rowParent, colParent, scaleBy, stepSize, units) Then
        RoundToNewSheet = "finround: " & FRLastMessage
        Exit Function
    End If
    FRNaive vals, nR, nC, scaleBy, stepSize, naive

    Dim ws As Worksheet
    Set ws = src.Worksheet.Parent.Worksheets.Add(After:=src.Worksheet)
    ws.Name = FreeSheetName(src.Worksheet.Parent, "Rounded")

    Dim fmt As String
    fmt = "#,##0"
    If decimals > 0 Then fmt = fmt & "." & String(decimals, "0")

    ws.Cells(1, 1).value = src.Worksheet.Name & " rounded"
    ws.Cells(1, 1).Font.Bold = True
    For j = 0 To nC - 1
        ws.Cells(2, j + 2).value = colLabels(j)
        ws.Cells(2, j + 2).Font.Bold = True
        ws.Cells(2, j + 2).HorizontalAlignment = xlRight
    Next j
    For i = 0 To nR - 1
        ws.Cells(i + 3, 1).value = rowLabels(i)
        If IsTotalRow(rowParent, nR, i) Then ws.Cells(i + 3, 1).Font.Bold = True
        For j = 0 To nC - 1
            With ws.Cells(i + 3, j + 2)
                If blank(i, j) Then
                    .value = ""
                Else
                    .value = units(i, j) * stepSize
                    .NumberFormat = fmt
                End If
                If IsTotalRow(rowParent, nR, i) Or IsTotalRow(colParent, nC, j) Then
                    .Font.Bold = True
                    .Interior.Color = FR_TOTAL_TINT
                End If
                If units(i, j) <> naive(i, j) And Not blank(i, j) Then
                    .Interior.Color = FR_MARK
                End If
            End With
        Next j
    Next i

    Dim atRow As Long
    atRow = nR + 5
    Dim moved As Long
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            If units(i, j) <> naive(i, j) Then moved = moved + 1
        Next j
    Next i

    Dim checks As Long, bad As Long
    checks = FRCheckCount(nR, nC, rowParent, colParent)
    bad = FRViolations(units, nR, nC, rowParent, colParent)

    WriteLine ws, atRow, "Rounded to " & Format$(stepSize, "0.######") & _
        IIf(scaleBy = 1, "", ", values divided by " & Format$(scaleBy, "#,##0"))
    WriteLine ws, atRow, "Structure: " & CountTotals(rowParent, nR) & " row total(s) and " & _
        CountTotals(colParent, nC) & " column total(s) over " & nR & "x" & nC & " figures."
    If checks = 0 Then
        WriteLine ws, atRow, "No totals were found, so every figure was simply rounded " & _
            "to its nearest value. Label the total rows, or use FinRoundToRange to " & _
            "name them yourself."
    ElseIf bad = 0 Then
        WriteLine ws, atRow, ChrW(10004) & " all " & checks & _
            " total checks foot exactly after rounding."
    Else
        WriteLine ws, atRow, ChrW(10008) & " " & bad & " of " & checks & _
            " total checks do NOT foot - do not publish this."
    End If
    WriteLine ws, atRow, moved & " of " & (nR * nC) & _
        " figures were moved off their nearest value (shaded) so the table foots."
    WriteLine ws, atRow, "Cost of consistency: total movement " & _
        Format$(FRMovement(vals, nR, nC, scaleBy, stepSize, units), "0.00") & _
        " steps against " & _
        Format$(FRMovement(vals, nR, nC, scaleBy, stepSize, naive), "0.00") & _
        " for straight rounding; no figure moved more than " & _
        Format$(FRWorstMove(vals, nR, nC, scaleBy, stepSize, units), "0.00") & " of a step."
    If Len(FRLastMessage) > 0 Then WriteLine ws, atRow, "! " & FRLastMessage
    For i = 0 To nR - 1
        If rowBad(i) Then
            WriteLine ws, atRow, "! row " & (i + 1) & " (" & rowLabels(i) & _
                ") looks like a total but no block of rows above it adds up to it" & _
                " - left out of the structure."
        End If
    Next i
    For j = 0 To nC - 1
        If colBad(j) Then
            WriteLine ws, atRow, "! column " & (j + 1) & " (" & colLabels(j) & _
                ") looks like a total but no block of columns to its left adds up to it."
        End If
    Next j

    ws.Columns(1).AutoFit
    ws.Range(ws.Cells(2, 2), ws.Cells(nR + 2, nC + 1)).Columns.AutoFit
    ws.Cells(1, 1).Select

    RoundToNewSheet = "Done. " & checks & " total checks, " & bad & " broken, " & _
                      moved & " figures moved. Written to sheet '" & ws.Name & "'."
End Function

' Round src into target, without touching the sheet layout. Both ranges
' include the headings and labels. Returns "" when it worked.
Public Function FinRoundToRange(src As Range, target As Range, _
                                ByVal scaleBy As Double, ByVal stepSize As Double) As String
    Dim vals() As Double, blank() As Boolean, units() As Double
    Dim rowLabels() As String, colLabels() As String
    Dim rowParent() As Long, colParent() As Long
    Dim rowBad() As Boolean, colBad() As Boolean
    Dim rowCand() As Boolean, colCand() As Boolean
    Dim nR As Long, nC As Long, r0 As Long, c0 As Long, i As Long, j As Long
    Dim problem As String

    problem = ReadBlock(src, vals, blank, rowLabels, colLabels, nR, nC, r0, c0)
    If Len(problem) > 0 Then
        FinRoundToRange = problem
        Exit Function
    End If
    ReDim rowCand(0 To nR - 1)
    ReDim colCand(0 To nC - 1)
    FRDetect vals, nR, nC, rowLabels, colLabels, (c0 = 2), (r0 = 2), _
             rowCand, colCand, 0, rowParent, colParent, rowBad, colBad
    If Not FRRound(vals, nR, nC, rowParent, colParent, scaleBy, stepSize, units) Then
        FinRoundToRange = FRLastMessage
        Exit Function
    End If
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            If blank(i, j) Then
                target.Cells(i + r0, j + c0).value = ""
            Else
                target.Cells(i + r0, j + c0).value = units(i, j) * stepSize
            End If
        Next j
    Next i
End Function

' Worksheet function: =FR_ROUND(A1:F10, 1000, 1), entered over a block the
' same size as the input. Headings and labels are passed through unchanged.
Public Function FR_ROUND(src As Range, Optional ByVal scaleBy As Double = 1, _
                         Optional ByVal decimals As Long = 0) As Variant
    Dim vals() As Double, blank() As Boolean, units() As Double
    Dim rowLabels() As String, colLabels() As String
    Dim rowParent() As Long, colParent() As Long
    Dim rowBad() As Boolean, colBad() As Boolean
    Dim rowCand() As Boolean, colCand() As Boolean
    Dim nR As Long, nC As Long, r0 As Long, c0 As Long, i As Long, j As Long
    Dim stepSize As Double, problem As String
    Dim out() As Variant

    problem = ReadBlock(src, vals, blank, rowLabels, colLabels, nR, nC, r0, c0)
    If Len(problem) > 0 Then
        FR_ROUND = "finround: " & problem
        Exit Function
    End If
    stepSize = 10 ^ (-decimals)
    ReDim rowCand(0 To nR - 1)
    ReDim colCand(0 To nC - 1)
    FRDetect vals, nR, nC, rowLabels, colLabels, (c0 = 2), (r0 = 2), _
             rowCand, colCand, 0, rowParent, colParent, rowBad, colBad
    If Not FRRound(vals, nR, nC, rowParent, colParent, scaleBy, stepSize, units) Then
        FR_ROUND = "finround: " & FRLastMessage
        Exit Function
    End If

    ReDim out(1 To src.Rows.Count, 1 To src.Columns.Count)
    For i = 1 To src.Rows.Count
        For j = 1 To src.Columns.Count
            out(i, j) = src.Cells(i, j).value
        Next j
    Next i
    For i = 0 To nR - 1
        For j = 0 To nC - 1
            If blank(i, j) Then
                out(i + r0, j + c0) = ""
            Else
                out(i + r0, j + c0) = units(i, j) * stepSize
            End If
        Next j
    Next i
    FR_ROUND = out
End Function

'==============================================================================
' Plumbing
'==============================================================================

' Pull the figures and labels out of a range. r0/c0 receive the 1-based
' position of the first figure within it, so 2 means there is a heading row
' or a label column.
Private Function ReadBlock(src As Range, vals() As Double, blank() As Boolean, _
                           rowLabels() As String, colLabels() As String, _
                           ByRef nR As Long, ByRef nC As Long, _
                           ByRef r0 As Long, ByRef c0 As Long) As String
    Dim i As Long, j As Long, value As Double, isBlank As Boolean

    r0 = 1
    c0 = 1
    If LooksLikeLabels(src, True) Then r0 = 2
    If LooksLikeLabels(src, False) Then c0 = 2

    nR = src.Rows.Count - (r0 - 1)
    nC = src.Columns.Count - (c0 - 1)
    If nR < 1 Or nC < 1 Then
        ReadBlock = "there are no figures in that selection"
        Exit Function
    End If

    ReDim vals(0 To nR - 1, 0 To nC - 1)
    ReDim blank(0 To nR - 1, 0 To nC - 1)
    ReDim rowLabels(0 To nR - 1)
    ReDim colLabels(0 To nC - 1)

    For i = 0 To nR - 1
        If c0 = 2 Then
            rowLabels(i) = Trim$(CStr(src.Cells(i + r0, 1).Text))
        Else
            rowLabels(i) = "row " & (i + 1)
        End If
    Next i
    For j = 0 To nC - 1
        If r0 = 2 Then
            colLabels(j) = Trim$(CStr(src.Cells(1, j + c0).Text))
        Else
            colLabels(j) = "column " & (j + 1)
        End If
    Next j

    For i = 0 To nR - 1
        For j = 0 To nC - 1
            If Not CellNumber(src.Cells(i + r0, j + c0), value, isBlank) Then
                ReadBlock = "cell " & src.Cells(i + r0, j + c0).Address(False, False) & _
                            " is not a number"
                Exit Function
            End If
            vals(i, j) = value
            blank(i, j) = isBlank
        Next j
    Next i
End Function

Private Function IsTotalRow(parent() As Long, ByVal n As Long, ByVal k As Long) As Boolean
    Dim i As Long
    For i = 0 To n - 1
        If parent(i) = k Then
            IsTotalRow = True
            Exit Function
        End If
    Next i
End Function

Private Function CountTotals(parent() As Long, ByVal n As Long) As Long
    Dim i As Long, total As Long
    For i = 0 To n - 1
        If IsTotalRow(parent, n, i) Then total = total + 1
    Next i
    CountTotals = total
End Function

Private Sub WriteLine(ws As Worksheet, ByRef atRow As Long, ByVal text As String)
    ws.Cells(atRow, 1).value = text
    atRow = atRow + 1
End Sub

Private Function FreeSheetName(book As Workbook, ByVal stem As String) As String
    Dim n As Long, candidate As String, ws As Worksheet, taken As Boolean
    n = 0
    Do
        n = n + 1
        If n = 1 Then
            candidate = stem
        Else
            candidate = stem & " " & n
        End If
        taken = False
        For Each ws In book.Worksheets
            If StrComp(ws.name, candidate, vbTextCompare) = 0 Then taken = True
        Next ws
    Loop While taken
    FreeSheetName = candidate
End Function
