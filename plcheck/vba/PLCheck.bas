Attribute VB_Name = "PLCheck"
'============================================================================
'  PLCheck  -  IFRS INR / Ind-AS Function-wise P&L reconciliation, in pure VBA
'----------------------------------------------------------------------------
'  Reproduces the plcheck tool entirely inside Excel: no Python, no add-ins,
'  no external libraries. It reads the P&L report and its three sources, then
'  builds a "Check" sheet (LC tie-out, GC FX conversion, GC consolidation and
'  net-profit reconciliation - all live formulas, with red highlighting) plus
'  a "Minority Interest" sheet.
'
'  SETUP (one time)
'    1. Open Excel, press Alt+F11 (Visual Basic editor).
'    2. File > Import File... and pick this PLCheck.bas
'       (or Insert > Module and paste this whole text in).
'    3. Back in Excel, add a sheet named "Control" with the four file paths:
'         B1: <full path to the P&L report .xlsx>
'         B2: <full path to Real Time TB .xlsx>
'         B3: <full path to Aggregate Expenses .xlsx>
'         B4: <full path to MA Rates .xlsx>
'    4. Run it: Developer > Macros > GenerateCheckFile > Run
'       (or add a button on a sheet and assign GenerateCheckFile to it).
'
'  The macro adds the sheets to THIS workbook; use File > Save As to keep a
'  copy. Re-running refreshes them. Edit the CONFIG section below if your
'  section names / chart of accounts differ.
'============================================================================
Option Explicit

' ---- sheet names -----------------------------------------------------------
Private Const SH_CHECK As String = "Check"
Private Const SH_TB As String = "Real Time TB"
Private Const SH_AGG As String = "Aggregate Exp"
Private Const SH_RATES As String = "MA rates"
Private Const SH_MIN As String = "Minority Interest"
Private Const SH_REPORT As String = "PL Report"

' ---- tunables --------------------------------------------------------------
Private Const TOL As Double = 0.5             ' highlight threshold
Private Const RATE_ROWS As Long = 150         ' FX-rate lookup window
Private Const DIV_ACCT As String = "332010"   ' dividend GL for Minority sheet
Private Const PL_DIGITS As String = "123"     ' P&L account leading digits

' ---- check-sheet row map ---------------------------------------------------
Private Const CK_TB As Long = 2
Private Const CK_AGG As Long = 3
Private Const CK_SUM As Long = 4
Private Const CK_BLOCK As Long = 5
Private Const CK_SUB As Long = 6
Private Const CK_NAME As Long = 7
Private Const CK_CCY As Long = 8
Private Const CK_DATA0 As Long = 9

' ---- report label columns --------------------------------------------------
Private Const RP_CAT As Long = 1      ' A  category / section
Private Const RP_ACC As Long = 2      ' B  GL account
Private Const RP_DESC As Long = 3     ' C  description
Private Const RP_NUM0 As Long = 4     ' D  first numeric column
Private Const OVERALL As String = "Overall Result"

' ---- module-level handoff (must be declared before any procedure) ----------
Private gNpRow As Long, gMiRow As Long, gDivRow As Long
Private gNoAgg As Boolean     ' True for a Nature-wise report (no Aggregate Exp)


'============================================================================
'  ENTRY POINT
'============================================================================
Public Sub GenerateCheckFile()
    Dim wb As Workbook: Set wb = ThisWorkbook
    Dim ctl As Worksheet
    On Error Resume Next
    Set ctl = wb.Worksheets("Control")
    On Error GoTo 0
    If ctl Is Nothing Then
        MsgBox "Create a 'Control' sheet with the four input file paths in " & _
               "B1:B4 (Report, TB, Agg, Rates).", vbExclamation: Exit Sub
    End If

    Dim pReport As String, pTB As String, pAgg As String, pRates As String
    pReport = Trim$(CStr(ctl.Range("B1").Value))
    pTB = Trim$(CStr(ctl.Range("B2").Value))
    pAgg = Trim$(CStr(ctl.Range("B3").Value))
    pRates = Trim$(CStr(ctl.Range("B4").Value))
    If Dir(pReport) = "" Or Dir(pTB) = "" Or Dir(pRates) = "" Then
        MsgBox "Report / TB / Rates file not found. Check Control!B1, B2, B4.", _
               vbExclamation: Exit Sub
    End If
    ' Aggregate Exp (B3) is optional: leave it blank for a Nature-wise report,
    ' where every line ties straight to the Real Time TB.
    gNoAgg = (Len(pAgg) = 0)
    If Not gNoAgg And Dir(pAgg) = "" Then
        MsgBox "Aggregate Expenses file not found (Control!B3). Leave B3 blank " & _
               "for a Nature-wise report.", vbExclamation: Exit Sub
    End If

    Application.ScreenUpdating = False
    Application.DisplayAlerts = False

    KillSheet wb, SH_CHECK: KillSheet wb, SH_MIN
    KillSheet wb, SH_TB: KillSheet wb, SH_AGG: KillSheet wb, SH_RATES
    KillSheet wb, SH_REPORT

    ImportFirstSheet wb, pTB, SH_TB
    If Not gNoAgg Then ImportFirstSheet wb, pAgg, SH_AGG
    ImportFirstSheet wb, pRates, SH_RATES
    ImportFirstSheet wb, pReport, SH_REPORT

    BuildCheck wb
    BuildMinority wb

    Application.DisplayAlerts = True
    Application.ScreenUpdating = True
    wb.Worksheets(SH_CHECK).Activate
    MsgBox "Done. Built '" & SH_CHECK & "' and '" & SH_MIN & "'. " & _
           "Use File > Save As to keep a copy.", vbInformation
End Sub


'============================================================================
'  BUILD THE CHECK SHEET
'============================================================================
Private Sub BuildCheck(wb As Workbook)
    Dim r As Worksheet: Set r = wb.Worksheets(SH_REPORT)

    ' header band (anchored on the block-label row)
    Dim rBlock As Long, rSub As Long, rName As Long, rCcy As Long, rData0 As Long
    rBlock = FindRowContaining(r, "LC - Balance"): If rBlock = 0 Then rBlock = 2
    rSub = rBlock + 1: rName = rBlock + 2: rCcy = rBlock + 3: rData0 = rBlock + 4

    Dim lastRCol As Long, lastRRow As Long
    lastRCol = r.Cells(rBlock, r.Columns.Count).End(xlToLeft).Column
    lastRRow = r.Cells(r.Rows.Count, RP_CAT).End(xlUp).Row

    ' entities, in report column order
    Dim codes As Object: Set codes = CreateObject("Scripting.Dictionary")
    Dim ccy As Object: Set ccy = CreateObject("Scripting.Dictionary")
    Dim nm As Object: Set nm = CreateObject("Scripting.Dictionary")
    Dim entOrder As Collection: Set entOrder = New Collection
    Dim c As Long, sub_ As String
    For c = RP_NUM0 To lastRCol
        sub_ = Trim$(CStr(r.Cells(rSub, c).Value))
        If Len(sub_) > 0 And StrComp(sub_, OVERALL, vbTextCompare) <> 0 Then
            If Not codes.Exists(sub_) Then
                codes(sub_) = c
                ccy(sub_) = Trim$(CStr(r.Cells(rCcy, c).Value))
                nm(sub_) = Trim$(CStr(r.Cells(rName, c).Value))
                entOrder.Add sub_
            End If
        End If
    Next c

    ' plan check columns: value (+ diff for checked blocks)
    Dim valCol As Object: Set valCol = CreateObject("Scripting.Dictionary")
    Dim diffCol As Object: Set diffCol = CreateObject("Scripting.Dictionary")
    Dim blkFirst As Object: Set blkFirst = CreateObject("Scripting.Dictionary")
    Dim blkLast As Object: Set blkLast = CreateObject("Scripting.Dictionary")
    Dim repToVal As Object: Set repToVal = CreateObject("Scripting.Dictionary")

    Dim ck As Worksheet
    Set ck = wb.Worksheets.Add(After:=wb.Worksheets(wb.Worksheets.Count))
    ck.Name = SH_CHECK

    Dim ckCol As Long: ckCol = 5
    Dim blk As String, key As String, isEnt As Boolean, checked As Boolean
    For c = RP_NUM0 To lastRCol
        blk = Trim$(CStr(r.Cells(rBlock, c).Value))
        sub_ = Trim$(CStr(r.Cells(rSub, c).Value))
        If Len(blk) > 0 Then
            isEnt = (StrComp(sub_, OVERALL, vbTextCompare) <> 0)
            checked = IsCheckedBlock(blk)
            key = blk & "|" & sub_
            valCol(key) = ckCol: repToVal(c) = ckCol
            ck.Cells(CK_BLOCK, ckCol).Value = blk
            ck.Cells(CK_SUB, ckCol).Value = sub_
            ck.Cells(CK_BLOCK, ckCol).Font.Bold = True
            If isEnt Then
                ck.Cells(CK_NAME, ckCol).Value = nm(sub_)
                ck.Cells(CK_CCY, ckCol).Value = ccy(sub_)
            End If
            If Not blkFirst.Exists(blk) Then blkFirst(blk) = ckCol
            blkLast(blk) = ckCol
            ckCol = ckCol + 1
            If checked And isEnt Then
                diffCol(key) = ckCol
                ck.Cells(CK_BLOCK, ckCol).Value = "Diff"
                ckCol = ckCol + 1
            End If
        End If
    Next c

    ck.Cells(CK_CCY, 3).Value = "GLACCOUNT"
    ck.Cells(CK_CCY, 4).Value = "GL Description | Currency"
    ck.Cells(CK_TB, 4).Value = SH_TB
    ck.Cells(CK_AGG, 4).Value = SH_AGG
    ck.Cells(CK_SUM, 4).Value = "Sum of Differences"

    ' source geometry
    Dim tbHdr As Long, tbAcct As Long, tbLastCol As Long, tbFirst As Long, tbLast As Long
    GetTBGeom wb.Worksheets(SH_TB), codes, tbHdr, tbAcct, tbLastCol, tbFirst, tbLast
    Dim rateFrom As Long, rateCol As Long, rateHdr As Long
    GetRatesGeom wb.Worksheets(SH_RATES), rateFrom, rateCol, rateHdr
    Dim aggMF As Long, aggML As Long, aggSub As Long, aggBlk As Object
    Dim hasAgg As Boolean: hasAgg = SheetExists(wb, SH_AGG)
    If hasAgg Then
        Set aggBlk = GetAggGeom(wb.Worksheets(SH_AGG), aggMF, aggML, aggSub)
    Else
        Set aggBlk = CreateObject("Scripting.Dictionary")   ' empty -> all tie to TB
    End If

    ' per-entity helper cells
    Dim i As Long, code As String, dc As Long, vc As Long
    For i = 1 To entOrder.Count
        code = entOrder(i)
        If diffCol.Exists("LC - Balance|" & code) Then
            dc = diffCol("LC - Balance|" & code): vc = valCol("LC - Balance|" & code)
            ck.Cells(CK_TB, dc).Formula = "=MATCH(" & ColL(vc) & "$" & CK_SUB & _
                ",'" & SH_TB & "'!$A$" & tbHdr & ":$" & ColL(tbLastCol) & "$" & tbHdr & ",0)"
            If hasAgg Then
                ck.Cells(CK_AGG, dc).Formula = "=MATCH(" & ColL(vc) & "$" & CK_SUB & _
                    ",'" & SH_AGG & "'!$" & ColL(aggMF) & "$" & aggSub & ":$" & ColL(aggML) & "$" & aggSub & ",0)"
            End If
        End If
        If diffCol.Exists("GC - Balance|" & code) Then
            dc = diffCol("GC - Balance|" & code): vc = valCol("GC - Balance|" & code)
            ck.Cells(CK_AGG, dc).Formula = "=VLOOKUP(" & ColL(vc) & "$" & CK_CCY & _
                ",'" & SH_RATES & "'!$" & ColL(rateFrom) & "$" & (rateHdr + 1) & ":$" & _
                ColL(rateCol) & "$" & (rateHdr + RATE_ROWS) & "," & (rateCol - rateFrom + 1) & ",FALSE)"
        End If
    Next i

    ' ranges for FX / consolidation
    Dim fxLast As String, consF As String, consL As String
    fxLast = ColL(LastPresent(blkLast, Array("LC - Balance", "LC - Consol")))
    consF = ColL(FirstPresent(blkFirst, Array("GC - Balance", "GC - Reclass", "GC - Elimination", "GC - Consol")))
    consL = ColL(LastPresent(blkLast, Array("GC - Balance", "GC - Reclass", "GC - Elimination", "GC - Consol")))

    ' data rows
    Dim diffCols As Object: Set diffCols = CreateObject("Scripting.Dictionary")
    Dim unmapped As Object: Set unmapped = CreateObject("Scripting.Dictionary")
    Dim rr As Long, cr As Long, cat As String, acc As String, isSub As Boolean
    Dim npSub As Long, npDet As Long, miSub As Long, miDet As Long
    Dim lastData As Long: lastData = CK_DATA0 - 1

    For rr = rData0 To lastRRow
        cat = Trim$(CStr(r.Cells(rr, RP_CAT).Value))
        acc = Trim$(CStr(r.Cells(rr, RP_ACC).Value))
        If Not (Len(cat) = 0 And Len(acc) = 0 And RowAllBlank(r, rr, RP_NUM0, lastRCol)) Then
            cr = CK_DATA0 + (rr - rData0): lastData = cr
            isSub = (Len(acc) = 0)
            If Len(cat) > 0 Then ck.Cells(cr, 2).Value = cat
            If Not isSub Then
                Dim cls As String: cls = CatClass(cat)
                If cls = "?" Then
                    unmapped(cat) = 1
                ElseIf Len(cls) > 0 Then
                    ck.Cells(cr, 1).Value = cls
                End If
                ck.Cells(cr, 3).Value = r.Cells(rr, RP_ACC).Value
                ck.Cells(cr, 4).Value = r.Cells(rr, RP_DESC).Value
            End If
            For c = RP_NUM0 To lastRCol
                If repToVal.Exists(c) Then ck.Cells(cr, repToVal(c)).Value = r.Cells(rr, c).Value
            Next c
            For i = 1 To entOrder.Count
                WriteDiffs ck, cr, entOrder(i), isSub, cat, valCol, diffCol, diffCols, _
                           tbHdr, tbLastCol, tbLast, aggBlk, fxLast, consF, consL
            Next i
            If StrComp(cat, "Net Profit", vbTextCompare) = 0 Then
                If isSub Then npSub = cr Else npDet = cr
            ElseIf StrComp(cat, "Minority Interest", vbTextCompare) = 0 Then
                If isSub Then miSub = cr Else miDet = cr
            End If
            If StrComp(acc, DIV_ACCT, vbTextCompare) = 0 Then gDivRow = cr
        End If
    Next rr

    ' Sum-of-Differences row
    Dim k As Variant
    For Each k In diffCols.Keys
        ck.Cells(CK_SUM, CLng(k)).Formula = _
            "=SUM(" & ColL(CLng(k)) & CK_DATA0 & ":" & ColL(CLng(k)) & lastData & ")"
    Next k

    BuildNetProfit ck, entOrder, valCol, diffCol, lastData, tbAcct, tbHdr, _
                   tbFirst, tbLast, wb.Worksheets(SH_TB)

    ' cosmetics + highlighting
    ck.Columns("A:D").ColumnWidth = 22
    Highlight ck, diffCols, lastData
    On Error Resume Next
    ck.Activate: ck.Cells(CK_DATA0, 5).Select: ActiveWindow.FreezePanes = True
    On Error GoTo 0

    gNpRow = IIf(npSub > 0, npSub, npDet)
    gMiRow = IIf(miSub > 0, miSub, miDet)

    If unmapped.Count > 0 Then
        MsgBox "Note: these report sections were not recognised, so their LC " & _
               "tie-out was skipped: " & Join(unmapped.Keys, ", ") & vbCrLf & _
               "Add them in CatClass / CatSource if needed.", vbExclamation
    End If
End Sub


'============================================================================
'  DIFFERENCE FORMULAS
'============================================================================
Private Sub WriteDiffs(ck As Worksheet, cr As Long, code As String, isSub As Boolean, _
                       cat As String, valCol As Object, diffCol As Object, diffCols As Object, _
                       tbHdr As Long, tbLastCol As Long, tbLast As Long, aggBlk As Object, _
                       fxLast As String, consF As String, consL As String)
    Dim dc As Long, vc As Long, src As String, fld As Variant, vL As String

    If diffCol.Exists("LC - Balance|" & code) Then
        dc = diffCol("LC - Balance|" & code): vc = valCol("LC - Balance|" & code)
        diffCols(dc) = 1: vL = ColL(vc)
        If Not isSub Then
            src = CatSource(cat)
            If src = "TB" Then
                ck.Cells(cr, dc).Formula = "=IFERROR(VLOOKUP($C" & cr & ",'" & SH_TB & _
                    "'!$A$" & tbHdr & ":$" & ColL(tbLastCol) & "$" & tbLast & ",Check!" & _
                    ColL(dc) & "$" & CK_TB & ",FALSE),0)-" & vL & cr
            ElseIf aggBlk.Exists(src) Then
                fld = Split(aggBlk(src), ",")    ' acct,subRow,firstData,lastData,lastEnt
                ck.Cells(cr, dc).Formula = "=IFERROR(VLOOKUP($C" & cr & ",'" & SH_AGG & _
                    "'!$" & ColL(CLng(fld(0))) & "$" & fld(1) & ":$" & ColL(CLng(fld(4))) & "$" & _
                    fld(3) & ",Check!" & ColL(dc) & "$" & CK_AGG & ",FALSE),0)-" & vL & cr
            End If
        End If
    End If

    If diffCol.Exists("GC - Balance|" & code) Then
        dc = diffCol("GC - Balance|" & code): vc = valCol("GC - Balance|" & code)
        diffCols(dc) = 1: vL = ColL(vc)
        ck.Cells(cr, dc).Formula = "=SUMIF($A$" & CK_SUB & ":$" & fxLast & "$" & CK_SUB & "," & _
            vL & "$" & CK_SUB & ",$A" & cr & ":$" & fxLast & cr & ")*Check!" & ColL(dc) & "$" & _
            CK_AGG & "-" & vL & cr
    End If

    If diffCol.Exists("GC - Total|" & code) Then
        dc = diffCol("GC - Total|" & code): vc = valCol("GC - Total|" & code)
        diffCols(dc) = 1: vL = ColL(vc)
        ck.Cells(cr, dc).Formula = "=SUMIF($" & consF & "$" & CK_SUB & ":$" & consL & "$" & CK_SUB & _
            "," & vL & "$" & CK_SUB & ",$" & consF & cr & ":$" & consL & cr & ")-" & vL & cr
    End If
End Sub


'============================================================================
'  NET-PROFIT RECONCILIATION BLOCK
'============================================================================
Private Sub BuildNetProfit(ck As Worksheet, entOrder As Collection, valCol As Object, _
                           diffCol As Object, lastData As Long, tbAcct As Long, tbHdr As Long, _
                           tbFirst As Long, tbLast As Long, tb As Worksheet)
    Dim s As Long: s = lastData + 2
    Dim rInc As Long, rExp As Long, rNp As Long, rCalc As Long, rTb As Long, rChk As Long
    rInc = s: rExp = s + 1: rNp = s + 2: rCalc = s + 4: rTb = s + 6: rChk = s + 7
    ck.Cells(rInc, 4).Value = "Income": ck.Cells(rExp, 4).Value = "Expense"
    ck.Cells(rNp, 4).Value = "Net Profit": ck.Cells(rCalc, 4).Value = "Calc Check"
    ck.Cells(rTb, 4).Value = "Net Profit as per Real Time TB"
    ck.Cells(rChk, 4).Value = "Check"
    ck.Cells(rInc, 4).Resize(8, 1).Font.Bold = True

    Dim acctL As String: acctL = ColL(tbAcct)
    Dim series As String, d As Long
    For d = 1 To Len(PL_DIGITS)
        If d > 1 Then series = series & "+"
        series = series & "(LEFT(TRIM('" & SH_TB & "'!$" & acctL & "$" & tbFirst & ":$" & acctL & _
                 "$" & tbLast & "),1)=""" & Mid$(PL_DIGITS, d, 1) & """)"
    Next d

    Dim i As Long, code As String, vL As String, dL As String, entL As String, pos As Variant, dc As Long
    For i = 1 To entOrder.Count
        code = entOrder(i)
        dc = diffCol("LC - Balance|" & code)
        vL = ColL(valCol("LC - Balance|" & code)): dL = ColL(dc)
        ck.Cells(rInc, dc).Formula = "=SUMIF($A:$A,$D" & rInc & "," & vL & ":" & vL & ")"
        ck.Cells(rExp, dc).Formula = "=SUMIF($A:$A,$D" & rExp & "," & vL & ":" & vL & ")"
        ck.Cells(rNp, dc).Formula = "=SUMIF($A:$A,$D" & rNp & "," & vL & ":" & vL & ")"
        ck.Cells(rCalc, dc).Formula = "=" & dL & rInc & "+" & dL & rExp & "+" & dL & rNp
        pos = Application.Match(code, tb.Rows(tbHdr), 0)
        If IsError(pos) Then
            ck.Cells(rTb, dc).Value = 0
        Else
            entL = ColL(CLng(pos))
            ck.Cells(rTb, dc).Formula = "=SUMPRODUCT((" & series & ")*('" & SH_TB & _
                "'!$" & entL & "$" & tbFirst & ":$" & entL & "$" & tbLast & "))"
        End If
        ck.Cells(rChk, dc).Formula = "=" & dL & rNp & "+" & dL & rTb
    Next i
End Sub


'============================================================================
'  MINORITY INTEREST SHEET
'============================================================================
Private Sub BuildMinority(wb As Workbook)
    If gNpRow = 0 Or gMiRow = 0 Then Exit Sub
    Dim ck As Worksheet: Set ck = wb.Worksheets(SH_CHECK)
    Dim ms As Worksheet: Set ms = wb.Worksheets.Add(After:=ck)
    ms.Name = SH_MIN
    ms.Range("A1").Value = "Code"
    ms.Range("B1").Value = "Net Profit as per GC Bal"
    ms.Range("C1").Value = "Div received - " & DIV_ACCT
    ms.Range("D1").Value = "Profit before Div"
    ms.Range("E1").Value = "Minority - Total"
    ms.Range("F1").Value = "Current Period %"
    ms.Range("A1:F1").Font.Bold = True

    Dim gcb As Object: Set gcb = ColsByHeader(ck, "GC - Balance")
    Dim gct As Object: Set gct = ColsByHeader(ck, "GC - Total")

    Dim outRow As Long: outRow = 2
    Dim key As Variant, code As String
    For Each key In gcb.Keys
        code = CStr(key)
        If gct.Exists(code) Then
            ms.Cells(outRow, 1).Value = code
            ms.Cells(outRow, 2).Formula = "='" & SH_CHECK & "'!" & ColL(CLng(gcb(code))) & gNpRow
            If gDivRow > 0 Then
                ms.Cells(outRow, 3).Formula = "='" & SH_CHECK & "'!" & ColL(CLng(gcb(code))) & gDivRow
            Else
                ms.Cells(outRow, 3).Value = 0
            End If
            ms.Cells(outRow, 4).Formula = "=B" & outRow & "+C" & outRow
            ms.Cells(outRow, 5).Formula = "='" & SH_CHECK & "'!" & ColL(CLng(gct(code))) & gMiRow
            ms.Cells(outRow, 6).Formula = "=IFERROR(E" & outRow & "/D" & outRow & ",0)"
            ms.Cells(outRow, 6).NumberFormat = "0.00%"
            outRow = outRow + 1
        End If
    Next key
    ms.Columns("A:F").ColumnWidth = 22
End Sub


'============================================================================
'  GEOMETRY HELPERS
'============================================================================
Private Sub GetTBGeom(tb As Worksheet, codes As Object, ByRef hdr As Long, ByRef acct As Long, _
                      ByRef lastCol As Long, ByRef firstData As Long, ByRef lastData As Long)
    hdr = FindRowWithAnyKey(tb, codes): If hdr = 0 Then hdr = 6
    Dim gcn As Range: Set gcn = tb.Cells.Find("Group Account Number", , , xlWhole, , , False)
    acct = IIf(gcn Is Nothing, 1, gcn.Column)
    lastCol = tb.Cells(hdr, tb.Columns.Count).End(xlToLeft).Column
    Dim lastUsed As Long: lastUsed = tb.UsedRange.Row + tb.UsedRange.Rows.Count - 1
    Dim rr As Long
    firstData = 0: lastData = hdr + 1
    For rr = hdr + 1 To lastUsed
        If LooksLikeAccount(tb.Cells(rr, acct).Value) Then
            If firstData = 0 Then firstData = rr
            lastData = rr
        End If
    Next rr
    If firstData = 0 Then firstData = hdr + 1
End Sub

Private Sub GetRatesGeom(rs As Worksheet, ByRef fromCol As Long, ByRef rateCol As Long, ByRef hdr As Long)
    Dim f As Range, rc As Range
    Set f = rs.Cells.Find("From", , , xlWhole, , , False)
    Set rc = rs.Cells.Find("Exch. Rate", , , xlWhole, , , False)
    fromCol = IIf(f Is Nothing, 3, f.Column)
    rateCol = IIf(rc Is Nothing, 5, rc.Column)
    hdr = IIf(f Is Nothing, 1, f.Row)
End Sub

Private Function GetAggGeom(ag As Worksheet, ByRef matchFirst As Long, _
                            ByRef matchLast As Long, ByRef subRow As Long) As Object
    Dim blocks As Object: Set blocks = CreateObject("Scripting.Dictionary")
    subRow = FindAggSubRow(ag): If subRow = 0 Then subRow = 7
    Dim labelRow As Long: labelRow = subRow - 1
    Dim lastUsedCol As Long: lastUsedCol = ag.UsedRange.Column + ag.UsedRange.Columns.Count - 1
    Dim lastUsedRow As Long: lastUsedRow = ag.UsedRange.Row + ag.UsedRange.Rows.Count - 1

    ' account columns
    Dim acctCols As Collection: Set acctCols = New Collection
    Dim rr As Long, c As Long
    For rr = 1 To lastUsedRow
        For c = 1 To lastUsedCol
            If StrComp(Trim$(CStr(ag.Cells(rr, c).Value)), "Group Account Number", vbTextCompare) = 0 Then
                acctCols.Add c
            End If
        Next c
    Next rr

    ' group the label row into contiguous blocks
    Dim grpStart As Long: grpStart = 1
    Dim prev As String: prev = Chr$(0)
    Dim firstDone As Boolean: firstDone = False
    Dim lab As String
    For c = 1 To lastUsedCol + 1
        If c <= lastUsedCol Then lab = Trim$(CStr(ag.Cells(labelRow, c).Value)) Else lab = Chr$(1)
        If lab <> prev Then
            If prev <> Chr$(0) And Len(prev) > 0 And (c - 1) >= grpStart Then
                RegisterAggBlock ag, blocks, prev, grpStart, c - 1, subRow, lastUsedRow, _
                                 acctCols, matchFirst, matchLast, firstDone
            End If
            grpStart = c: prev = lab
        End If
    Next c
    Set GetAggGeom = blocks
End Function

Private Sub RegisterAggBlock(ag As Worksheet, blocks As Object, label As String, c1 As Long, c2 As Long, _
                             subRow As Long, lastUsedRow As Long, acctCols As Collection, _
                             ByRef matchFirst As Long, ByRef matchLast As Long, ByRef firstDone As Boolean)
    Dim c As Long, firstEnt As Long, lastEnt As Long
    firstEnt = 0: lastEnt = 0
    For c = c1 To c2
        If IsEntityCode(Trim$(CStr(ag.Cells(subRow, c).Value))) Then
            If firstEnt = 0 Then firstEnt = c
            lastEnt = c
        End If
    Next c
    If firstEnt = 0 Then Exit Sub
    Dim acct As Long: acct = 0
    Dim k As Variant
    For Each k In acctCols
        If CLng(k) < firstEnt And CLng(k) > acct Then acct = CLng(k)
    Next k
    If acct = 0 Then Exit Sub
    Dim rr As Long, fData As Long, lData As Long
    fData = 0: lData = subRow + 1
    For rr = subRow + 1 To lastUsedRow
        If LooksLikeAccount(ag.Cells(rr, acct).Value) Then
            If fData = 0 Then fData = rr
            lData = rr
        End If
    Next rr
    If fData = 0 Then fData = subRow + 1
    blocks(label) = acct & "," & subRow & "," & fData & "," & lData & "," & lastEnt
    If Not firstDone Then matchFirst = acct: matchLast = lastEnt: firstDone = True
End Sub

Private Function FindAggSubRow(ag As Worksheet) As Long
    ' the sub-header row is the one containing "Company" (sits above the data)
    Dim f As Range: Set f = ag.Cells.Find("Company", , , xlWhole, , , False)
    FindAggSubRow = IIf(f Is Nothing, 0, f.Row)
End Function


'============================================================================
'  CONFIG: section name -> classification / source  (edit here if names differ)
'============================================================================
Private Function CatClass(ByVal cat As String) As String
    Select Case LCase$(Trim$(cat))
        Case "revenue", "income", "other income": CatClass = "Income"
        Case "net profit": CatClass = "Net Profit"
        Case "minority interest": CatClass = ""
        Case "cost of production", "sales", "general administration", _
             "software development exp", "sales & marketing cost", "administration cost", _
             "depreciation", "provision for tax", "interest", "interest exp", _
             "interest expense", "finance cost", "finance costs", "provision for investment", _
             "employee benefit expenses", "cost of technical sub-contractors", _
             "travel expenses", "software packages for own use", "communication expenses", _
             "professional charges", "others"
            CatClass = "Expense"
        ' Nature-wise report (no Aggregate Exp): an unrecognised line is an
        ' expense tied to the TB. Function-wise: report it as unmapped.
        Case Else: CatClass = IIf(gNoAgg, "Expense", "?")
    End Select
End Function

Private Function CatSource(ByVal cat As String) As String
    Select Case LCase$(Trim$(cat))
        Case "revenue", "income", "other income", "provision for tax", "interest", _
             "interest exp", "interest expense", "finance cost", "finance costs", _
             "provision for investment", "depreciation", _
             "employee benefit expenses", "cost of technical sub-contractors", _
             "travel expenses", "software packages for own use", "communication expenses", _
             "professional charges", "others": CatSource = "TB"
        Case "cost of production", "software development exp": CatSource = "Cost of revenue"
        Case "sales", "sales & marketing cost": CatSource = "Sales & Marketing"
        Case "general administration", "administration cost": CatSource = "General Administration"
        Case Else: CatSource = IIf(gNoAgg, "TB", "")
    End Select
End Function

Private Function SheetExists(wb As Workbook, ByVal nm As String) As Boolean
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = wb.Worksheets(nm)
    On Error GoTo 0
    SheetExists = Not ws Is Nothing
End Function

Private Function IsCheckedBlock(ByVal blk As String) As Boolean
    Select Case LCase$(Trim$(blk))
        Case "lc - balance", "gc - balance", "gc - total": IsCheckedBlock = True
        Case Else: IsCheckedBlock = False
    End Select
End Function


'============================================================================
'  SMALL HELPERS
'============================================================================
Private Function IsEntityCode(ByVal v As String) As Boolean
    Dim s As String: s = LCase$(Trim$(v))
    IsEntityCode = (Len(s) > 0) And (s <> "company") And (s <> OVERALL) _
                   And (s <> "overall result") And (s <> "group account number")
End Function

Private Function LooksLikeAccount(ByVal v As Variant) As Boolean
    If IsEmpty(v) Or IsNull(v) Then Exit Function
    If IsNumeric(v) Then LooksLikeAccount = True: Exit Function
    Dim s As String: s = Trim$(CStr(v))
    LooksLikeAccount = (Len(s) > 0) And IsNumeric(s) And (InStr(s, ".") = 0)
End Function

Private Function ColL(ByVal c As Long) As String
    Dim s As String
    Do While c > 0
        s = Chr$(65 + ((c - 1) Mod 26)) & s
        c = (c - 1) \ 26
    Loop
    ColL = s
End Function

Private Function FindRowContaining(ws As Worksheet, ByVal txt As String) As Long
    Dim f As Range: Set f = ws.Cells.Find(txt, , , xlWhole, , , False)
    FindRowContaining = IIf(f Is Nothing, 0, f.Row)
End Function

Private Function FindRowWithAnyKey(ws As Worksheet, codes As Object) As Long
    Dim k As Variant, f As Range
    For Each k In codes.Keys
        Set f = ws.Cells.Find(CStr(k), , , xlWhole, , , False)
        If Not f Is Nothing Then FindRowWithAnyKey = f.Row: Exit Function
    Next k
End Function

Private Function RowAllBlank(ws As Worksheet, rr As Long, c1 As Long, c2 As Long) As Boolean
    Dim c As Long
    For c = c1 To c2
        If Len(Trim$(CStr(ws.Cells(rr, c).Value))) > 0 Then Exit Function
    Next c
    RowAllBlank = True
End Function

Private Function FirstPresent(d As Object, arr As Variant) As Long
    Dim i As Long, best As Long: best = 0
    For i = LBound(arr) To UBound(arr)
        If d.Exists(arr(i)) Then
            If best = 0 Or CLng(d(arr(i))) < best Then best = CLng(d(arr(i)))
        End If
    Next i
    FirstPresent = best
End Function

Private Function LastPresent(d As Object, arr As Variant) As Long
    Dim i As Long, best As Long: best = 0
    For i = LBound(arr) To UBound(arr)
        If d.Exists(arr(i)) Then
            If CLng(d(arr(i))) > best Then best = CLng(d(arr(i)))
        End If
    Next i
    LastPresent = best
End Function

Private Function ColsByHeader(ck As Worksheet, ByVal blockLabel As String) As Object
    Dim d As Object: Set d = CreateObject("Scripting.Dictionary")
    Dim lastC As Long: lastC = ck.Cells(CK_BLOCK, ck.Columns.Count).End(xlToLeft).Column
    Dim c As Long, sub_ As String
    For c = 5 To lastC
        If StrComp(Trim$(CStr(ck.Cells(CK_BLOCK, c).Value)), blockLabel, vbTextCompare) = 0 Then
            sub_ = Trim$(CStr(ck.Cells(CK_SUB, c).Value))
            If IsEntityCode(sub_) Then If Not d.Exists(sub_) Then d(sub_) = c
        End If
    Next c
    Set ColsByHeader = d
End Function

Private Sub Highlight(ck As Worksheet, diffCols As Object, lastData As Long)
    Dim k As Variant, rng As Range, cl As String
    For Each k In diffCols.Keys
        cl = ColL(CLng(k))
        Set rng = ck.Range(cl & CK_SUM & ":" & cl & (lastData + 12))
        rng.FormatConditions.Delete
        rng.FormatConditions.Add Type:=xlExpression, _
            Formula1:="=AND(" & cl & CK_SUM & "<>"""",ABS(" & cl & CK_SUM & ")>" & TOL & ")"
        With rng.FormatConditions(rng.FormatConditions.Count)
            .Interior.Color = RGB(255, 199, 206)
            .Font.Color = RGB(156, 0, 6)
        End With
    Next k
End Sub

Private Sub KillSheet(wb As Workbook, ByVal nm As String)
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = wb.Worksheets(nm)
    If Not ws Is Nothing Then ws.Delete
    On Error GoTo 0
End Sub

Private Sub ImportFirstSheet(wb As Workbook, ByVal path As String, ByVal newName As String)
    Dim src As Workbook
    Set src = Workbooks.Open(Filename:=path, ReadOnly:=True, UpdateLinks:=0)
    src.Worksheets(1).Copy After:=wb.Worksheets(wb.Worksheets.Count)
    wb.Worksheets(wb.Worksheets.Count).Name = newName
    src.Close SaveChanges:=False
End Sub
