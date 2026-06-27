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
'    3. Back in Excel, add a sheet named "Control" with the input file paths:
'         B1: <full path to the P&L report .xlsx>
'         B2: <full path to Real Time TB .xlsx>
'         B3: <full path to Aggregate Expenses .xlsx>   (blank for Nature-wise)
'         B4: <full path to MA Rates .xlsx>
'         B5: <group currency INR/USD>                  (optional; auto-detected)
'         B6: <full path to Consolidation entries .xlsx>(optional; LC-Consol tie-out)
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
Private Const SH_CONSOL As String = "Consol Entries"   ' manual consol-entry tracker
Private Const SH_COVERAGE As String = "Entity Coverage" ' company-code coverage
Private Const SH_LCCONSOL As String = "LC-Consol Check"  ' LC-Consol tie-out tab

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
Private gGc As String         ' group/consolidation currency (INR default, or USD)
Private gHasConsol As Boolean ' True when a consolidation-entry tracker is supplied
' geometry of the consol tracker (set in GenerateCheckFile, used in WriteDiffs)
Private gCcConcat As Long, gCcVal1 As Long, gCcVal2 As Long
Private gCcFirst As Long, gCcLast As Long, gCcFunc As Long


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

    Dim pReport As String, pTB As String, pAgg As String, pRates As String, pConsol As String
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
    ' group/consolidation currency: Control!B5 overrides; blank = auto-detect
    ' from the report (INR vs USD). GC figures use  local->INR / GC->INR.
    gGc = UCase$(Trim$(CStr(ctl.Range("B5").Value)))
    ' Consolidation-entry tracker (B6) is optional: when given, LC - Consol is
    ' tied out to it (comp-code + account = the tracker's "Concatenate" key).
    pConsol = Trim$(CStr(ctl.Range("B6").Value))
    gHasConsol = (Len(pConsol) > 0)
    If gHasConsol And Dir(pConsol) = "" Then
        MsgBox "Consolidation entry tracker not found (Control!B6). Leave B6 " & _
               "blank to skip the LC-Consol check.", vbExclamation: Exit Sub
    End If

    ' Remember Excel's current state so we can restore it exactly afterwards.
    Dim savedCalc As XlCalculation, savedEvents As Boolean
    savedCalc = Application.Calculation
    savedEvents = Application.EnableEvents

    ' From here on, if anything errors we still restore Excel (see CleanFail).
    On Error GoTo CleanFail
    Application.ScreenUpdating = False
    Application.DisplayAlerts = False
    Application.EnableEvents = False
    ' Switch off automatic recalculation during the build: otherwise Excel
    ' re-runs every formula on the sheet after EACH formula we write, which is
    ' what makes a large report take many minutes. We recalc once at the end.
    Application.Calculation = xlCalculationManual

    KillSheet wb, SH_CHECK: KillSheet wb, SH_MIN: KillSheet wb, SH_COVERAGE
    KillSheet wb, SH_TB: KillSheet wb, SH_AGG: KillSheet wb, SH_RATES
    KillSheet wb, SH_REPORT: KillSheet wb, SH_CONSOL: KillSheet wb, SH_LCCONSOL

    ImportFirstSheet wb, pTB, SH_TB
    If Not gNoAgg Then ImportFirstSheet wb, pAgg, SH_AGG
    ImportFirstSheet wb, pRates, SH_RATES
    ImportFirstSheet wb, pReport, SH_REPORT
    If gHasConsol Then
        ImportConsolSheet wb, pConsol, SH_CONSOL
        GetConsolGeom wb.Worksheets(SH_CONSOL), gCcConcat, gCcVal1, gCcVal2, _
                      gCcFirst, gCcLast, gCcFunc
    End If

    BuildCheck wb
    BuildMinority wb
    BuildLcConsolCheck wb
    BuildEntityCoverage wb

    Application.Calculate            ' recalculate the whole workbook once, now

    ' restore Excel to exactly how we found it
    Application.Calculation = savedCalc
    Application.EnableEvents = savedEvents
    Application.DisplayAlerts = True
    Application.ScreenUpdating = True

    wb.Worksheets(SH_CHECK).Activate
    MsgBox "Done. Built '" & SH_CHECK & "', '" & SH_MIN & "', '" & SH_LCCONSOL & _
           "' and '" & SH_COVERAGE & "'. Use File > Save As to keep a copy.", _
           vbInformation
    Exit Sub

CleanFail:
    ' Something went wrong mid-run: always put Excel back to normal so you are
    ' never left stuck in manual calculation / with screen updating off.
    Application.Calculation = savedCalc
    Application.EnableEvents = savedEvents
    Application.DisplayAlerts = True
    Application.ScreenUpdating = True
    MsgBox "PLCheck stopped before finishing: " & Err.Description, vbExclamation
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
    Dim repCol As Object: Set repCol = CreateObject("Scripting.Dictionary") ' "blk|sub"->report col

    Dim ck As Worksheet
    Set ck = wb.Worksheets.Add(After:=wb.Worksheets(wb.Worksheets.Count))
    ck.Name = SH_CHECK

    Dim ckCol As Long: ckCol = 5
    Dim blk As String, key As String, isEnt As Boolean, checked As Boolean
    For c = RP_NUM0 To lastRCol
        blk = CanonicalBlock(CStr(r.Cells(rBlock, c).Value))
        sub_ = Trim$(CStr(r.Cells(rSub, c).Value))
        If Len(blk) > 0 Then
            isEnt = (StrComp(sub_, OVERALL, vbTextCompare) <> 0)
            checked = IsCheckedBlock(blk)
            key = blk & "|" & sub_
            valCol(key) = ckCol: repToVal(c) = ckCol: repCol(key) = c
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

    ' group currency: auto-detect from the report unless Control!B5 set it
    Dim ratesD As Object: Set ratesD = RatesDict(wb.Worksheets(SH_RATES))
    If Len(gGc) = 0 Then _
        gGc = DetectGc(r, rData0, lastRRow, entOrder, ccy, repCol, ratesD)
    ' Does the group currency have its own row in the rates table? The table
    ' quotes everything TO the base currency (e.g. INR), which has no row, so the
    ' rate already converts to it (no division). Only a quoted GC (e.g. USD) is
    ' cross-divided.
    Dim gcInTable As Boolean: gcInTable = ratesD.Exists(UCase$(Trim$(gGc)))

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
            Dim rng As String, idx As Long, numr As String
            rng = "'" & SH_RATES & "'!$" & ColL(rateFrom) & "$" & (rateHdr + 1) & _
                  ":$" & ColL(rateCol) & "$" & (rateHdr + RATE_ROWS)
            idx = rateCol - rateFrom + 1
            numr = "VLOOKUP(" & ColL(vc) & "$" & CK_CCY & "," & rng & "," & idx & ",FALSE)"
            If gcInTable Then
                ' cross-rate: local->base / GC->base
                ck.Cells(CK_AGG, dc).Formula = "=" & numr & _
                    "/VLOOKUP(""" & gGc & """," & rng & "," & idx & ",FALSE)"
            Else
                ' GC is the table's base currency: the rate already converts to it
                ck.Cells(CK_AGG, dc).Formula = "=" & numr
            End If
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
                           tbHdr, tbLastCol, tbLast, aggBlk, fxLast, consF, consL, _
                           IsPlAcct(acc), FuncCode(cat)
            Next i
            If StrComp(cat, "Net Profit", vbTextCompare) = 0 Then
                If isSub Then npSub = cr Else npDet = cr
            ElseIf IsMinority(cat) Then
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
                       fxLast As String, consF As String, consL As String, isPl As Boolean, _
                       fcode As String)
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

    ' (LC - Consol is reconciled on its own dedicated sheet, not here.)

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

' One SUMIF/SUMIFS term for the LC-Consol tie-out. With a functional code it
' matches account + Functional Group (function-wise split); without, it matches
' account only (nature-wise total).
Private Function ConsolSum(ByVal ccR As String, ByVal crit As String, ByVal fR As String, _
                           ByVal fcode As String, ByVal vCol As Long) As String
    Dim vR As String
    vR = "'" & SH_CONSOL & "'!$" & ColL(vCol) & "$" & gCcFirst & ":$" & ColL(vCol) & "$" & gCcLast
    If Len(fR) > 0 Then
        ConsolSum = "SUMIFS(" & vR & "," & ccR & "," & crit & "," & fR & ",""" & fcode & """)"
    Else
        ConsolSum = "SUMIF(" & ccR & "," & crit & "," & vR & ")"
    End If
End Function


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
    ' Needs both the Net Profit and Minority Interest section rows. If either
    ' wasn't found in the report, say which one, rather than silently skipping
    ' the sheet — usually the section heading is worded differently.
    If gNpRow = 0 Or gMiRow = 0 Then
        Dim miss As String
        If gNpRow = 0 Then miss = "'Net Profit'"
        If gMiRow = 0 Then miss = miss & IIf(Len(miss) > 0, " and ", "") & "'Minority Interest'"
        MsgBox "Minority Interest sheet skipped: could not find the " & miss & _
               " section in the report. Check that heading in column A of the " & _
               "PL Report (it is matched even with a small typo, but a very " & _
               "different wording won't be recognised).", vbExclamation
        Exit Sub
    End If
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
'  LC - CONSOL CHECK  (dedicated tie-out tab)
'============================================================================
' Per company + P&L account: the report's LC-Consol value (from the Check tab)
' vs the tracker's net posting (Debit - Credit). Function-wise rows match their
' COS/S&M/G&A slice; nature-wise rows match the account total. Banner + red.
Private Sub BuildLcConsolCheck(wb As Workbook)
    If Not gHasConsol Then Exit Sub
    Dim ck As Worksheet: Set ck = wb.Worksheets(SH_CHECK)
    Dim cs As Worksheet: Set cs = wb.Worksheets(SH_CONSOL)

    ' LC-Consol value columns on the Check tab (block row CK_BLOCK, code CK_SUB)
    Dim ents As Collection: Set ents = New Collection
    Dim lcCol As Object: Set lcCol = CreateObject("Scripting.Dictionary")
    Dim c As Long, lastC As Long
    lastC = ck.Cells(CK_BLOCK, ck.Columns.Count).End(xlToLeft).Column
    For c = 5 To lastC
        If LCase$(Trim$(CStr(ck.Cells(CK_BLOCK, c).Value))) = "lc - consol" Then
            Dim cd As String: cd = Trim$(CStr(ck.Cells(CK_SUB, c).Value))
            If Len(cd) > 0 And StrComp(cd, OVERALL, vbTextCompare) <> 0 _
               And Not lcCol.Exists(cd) Then
                lcCol(cd) = c: ents.Add cd
            End If
        End If
    Next c
    If ents.Count = 0 Then Exit Sub

    Dim ws As Worksheet
    Set ws = wb.Worksheets.Add(After:=wb.Worksheets(wb.Worksheets.Count))
    ws.Name = SH_LCCONSOL
    ws.Cells(2, 1).Value = "Company": ws.Cells(2, 2).Value = "Section"
    ws.Cells(2, 3).Value = "GLACCOUNT": ws.Cells(2, 4).Value = "GL Description"
    ws.Cells(2, 5).Value = "LC-Consol value": ws.Cells(2, 6).Value = "Diff"
    ws.Range("A2:F2").Font.Bold = True

    Dim ccR As String
    ccR = "'" & SH_CONSOL & "'!$" & ColL(gCcConcat) & "$" & gCcFirst & _
          ":$" & ColL(gCcConcat) & "$" & gCcLast

    Dim lastData As Long: lastData = ck.Cells(ck.Rows.Count, 3).End(xlUp).Row
    Dim r As Long, k As Long: k = 3
    For r = CK_DATA0 To lastData
        Dim acc As String: acc = Trim$(CStr(ck.Cells(r, 3).Value))
        If Len(acc) > 0 And IsPlAcct(acc) Then
            Dim cat As String: cat = Trim$(CStr(ck.Cells(r, 2).Value))
            Dim fcode As String: fcode = FuncCode(cat)
            Dim fR As String
            If Len(fcode) > 0 And gCcFunc > 0 Then
                fR = "'" & SH_CONSOL & "'!$" & ColL(gCcFunc) & "$" & gCcFirst & _
                     ":$" & ColL(gCcFunc) & "$" & gCcLast
            Else
                fR = ""
            End If
            Dim jj As Long
            For jj = 1 To ents.Count
                Dim cd2 As String: cd2 = ents(jj)
                Dim repVal As Double: repVal = Val0(ck.Cells(r, lcCol(cd2)).Value)
                Dim trk As Double: trk = ConsolNet(cs, cd2, acc, fcode)
                ' show only lines that have a balance on either side
                If Abs(repVal) > TOL Or Abs(trk) > TOL Then
                    Dim crit As String, s1 As String, s2 As String
                    ws.Cells(k, 1).Value = cd2
                    ws.Cells(k, 2).Value = cat
                    ws.Cells(k, 3).Value = ck.Cells(r, 3).Value
                    ws.Cells(k, 4).Value = ck.Cells(r, 4).Value
                    ws.Cells(k, 5).Formula = "='" & SH_CHECK & "'!" & ColL(lcCol(cd2)) & r
                    crit = """" & cd2 & """&$C" & k
                    s1 = ConsolSum(ccR, crit, fR, fcode, gCcVal1)
                    If gCcVal2 > 0 Then s2 = "-" & ConsolSum(ccR, crit, fR, fcode, gCcVal2) Else s2 = ""
                    ws.Cells(k, 6).Formula = "=" & s1 & s2 & "-E" & k
                    k = k + 1
                End If
            Next jj
        End If
    Next r
    Dim lastK As Long: lastK = k - 1

    If lastK < 3 Then
        ws.Cells(1, 1).Value = "LC - Consol: no consolidation entries with a balance"
        ws.Cells(1, 1).Font.Bold = True
        Exit Sub
    End If

    With ws.Range("F3:F" & lastK)
        .FormatConditions.Delete
        .FormatConditions.Add Type:=xlExpression, Formula1:="=ABS(F3)>" & TOL
        .FormatConditions(1).Interior.Color = RGB(255, 199, 206)
        .FormatConditions(1).Font.Color = RGB(156, 0, 6)
    End With
    ws.Range("B1").Formula = "=SUMPRODUCT(--(ABS(F3:F" & lastK & ")>" & TOL & "))"
    ws.Range("A1").Formula = "=IF(B1=0,""LC - Consol: ALL entries tie""," & _
        """LC - Consol: ""&B1&"" difference(s) NOT tied - see red"")"
    ws.Range("A1").Font.Bold = True
    With ws.Range("A1:B1")
        .FormatConditions.Delete
        .FormatConditions.Add Type:=xlExpression, Formula1:="=$B$1>0"
        .FormatConditions(1).Interior.Color = RGB(255, 199, 206)
        .FormatConditions(1).Font.Color = RGB(156, 0, 6)
    End With
    ws.Columns("B").ColumnWidth = 22: ws.Columns("D").ColumnWidth = 30
End Sub

' Tracker net posting (Debit - Credit) for one company+account, computed at
' build time so we can list only lines that actually have a balance. Matches the
' Functional Group when a code is given (function-wise split).
Private Function ConsolNet(cs As Worksheet, ByVal code As String, _
                           ByVal acct As String, ByVal fcode As String) As Double
    Dim want As String: want = UCase$(Trim$(code) & Trim$(acct))
    Dim r As Long, tot As Double: tot = 0
    For r = gCcFirst To gCcLast
        If UCase$(Trim$(CStr(cs.Cells(r, gCcConcat).Value))) = want Then
            Dim ok As Boolean: ok = True
            If Len(fcode) > 0 And gCcFunc > 0 Then
                ok = (StrComp(Trim$(CStr(cs.Cells(r, gCcFunc).Value)), fcode, vbTextCompare) = 0)
            End If
            If ok Then
                tot = tot + Val0(cs.Cells(r, gCcVal1).Value)
                If gCcVal2 > 0 Then tot = tot - Val0(cs.Cells(r, gCcVal2).Value)
            End If
        End If
    Next r
    ConsolNet = tot
End Function


'============================================================================
'  ENTITY (COMPANY-CODE) COVERAGE
'============================================================================
' Lists the company codes in each input and flags any present in one source but
' missing from another (e.g. a TB-only typo like ATI10N).
Private Sub BuildEntityCoverage(wb As Workbook)
    Dim srcName() As String, srcCodes() As Collection, n As Long
    Dim hasAg As Boolean: hasAg = SheetExists(wb, SH_AGG)
    n = IIf(hasAg, 3, 2)
    ReDim srcName(1 To n): ReDim srcCodes(1 To n)
    srcName(1) = "PL Report":    Set srcCodes(1) = ReportCodes(wb.Worksheets(SH_REPORT))
    srcName(2) = "Real Time TB": Set srcCodes(2) = CodesAfterCompany(wb.Worksheets(SH_TB))
    If hasAg Then
        srcName(3) = "Aggregate Exp": Set srcCodes(3) = CodesAfterCompany(wb.Worksheets(SH_AGG))
    End If

    Dim ws As Worksheet: Set ws = wb.Worksheets.Add(After:=wb.Worksheets(wb.Worksheets.Count))
    ws.Name = SH_COVERAGE
    Dim j As Long, k As Long
    ' raw list per source (one column each), starting row 2
    For j = 1 To n
        ws.Cells(2, j).Value = srcName(j): ws.Cells(2, j).Font.Bold = True
        For k = 1 To srcCodes(j).Count
            ws.Cells(2 + k, j).Value = srcCodes(j)(k)
        Next k
    Next j

    ' union reconciliation matrix
    Dim base As Long: base = n + 2
    ws.Cells(2, base).Value = "Company Code": ws.Cells(2, base).Font.Bold = True
    For j = 1 To n
        ws.Cells(2, base + j).Value = "In " & srcName(j): ws.Cells(2, base + j).Font.Bold = True
    Next j
    Dim noteCol As Long: noteCol = base + n + 1
    ws.Cells(2, noteCol).Value = "Note (where missing)": ws.Cells(2, noteCol).Font.Bold = True

    Dim union As Collection: Set union = New Collection
    For j = 1 To n
        For k = 1 To srcCodes(j).Count
            AddUnique union, CStr(srcCodes(j)(k))
        Next k
    Next j

    Dim rr As Long: rr = 3
    Dim bad As Long: bad = 0
    Dim u As Variant
    For Each u In union
        Dim miss As String: miss = ""
        ws.Cells(rr, base).Value = u
        For j = 1 To n
            If InColl(srcCodes(j), CStr(u)) Then
                ws.Cells(rr, base + j).Value = "Yes"
            Else
                ws.Cells(rr, base + j).Value = "-"
                miss = miss & IIf(Len(miss) > 0, ", ", "") & srcName(j)
            End If
        Next j
        If Len(miss) = 0 Then
            ws.Cells(rr, noteCol).Value = "in all sources"
        Else
            ws.Cells(rr, noteCol).Value = "missing from: " & miss
            bad = bad + 1
            Dim cc As Long
            For cc = base To noteCol
                ws.Cells(rr, cc).Interior.Color = RGB(255, 199, 206)
                ws.Cells(rr, cc).Font.Color = RGB(156, 0, 6)
            Next cc
        End If
        rr = rr + 1
    Next u

    ws.Cells(1, 1).Value = IIf(bad = 0, _
        "Entity coverage: ALL company codes present in every source", _
        "Entity coverage: " & bad & " code(s) missing from some source - see red")
    ws.Cells(1, 1).Font.Bold = True
    If bad > 0 Then ws.Cells(1, 1).Font.Color = RGB(156, 0, 6)
    ws.Columns("A:Z").AutoFit
End Sub

' company codes to the right of each "Company" label (TB / Aggregate Exp)
Private Function CodesAfterCompany(ws As Worksheet) As Collection
    Dim res As Collection: Set res = New Collection
    Dim ur As Range: Set ur = ws.UsedRange
    Dim rN As Long, cN As Long, r As Long, c As Long, cc As Long
    rN = ur.Row + ur.Rows.Count - 1: cN = ur.Column + ur.Columns.Count - 1
    For r = ur.Row To rN
        For c = ur.Column To cN
            If LCase$(Trim$(CStr(ws.Cells(r, c).Value))) = "company" Then
                For cc = c + 1 To cN
                    Dim s As String: s = Trim$(CStr(ws.Cells(r, cc).Value))
                    If Len(s) = 0 Or LCase$(s) = "overall result" Then Exit For
                    If IsEntityCode(s) Then AddUnique res, s
                Next cc
            End If
        Next c
    Next r
    Set CodesAfterCompany = res
End Function

' company codes from the PL report's sub-header row (under the block labels)
Private Function ReportCodes(ws As Worksheet) As Collection
    Dim res As Collection: Set res = New Collection
    Dim blr As Long: blr = FindRowContaining(ws, "LC - Balance")
    If blr = 0 Then blr = 2
    Dim ur As Range: Set ur = ws.UsedRange
    Dim cN As Long: cN = ur.Column + ur.Columns.Count - 1
    Dim c As Long
    ' scan only the data columns (D onward), like the main entity detection, so
    ' a label in the left columns of the sub-header row isn't taken as a code.
    For c = RP_NUM0 To cN
        Dim s As String: s = Trim$(CStr(ws.Cells(blr + 1, c).Value))
        If IsEntityCode(s) Then AddUnique res, s
    Next c
    Set ReportCodes = res
End Function

Private Sub AddUnique(coll As Collection, ByVal s As String)
    If Not InColl(coll, s) Then coll.Add s
End Sub

Private Function InColl(coll As Collection, ByVal s As String) As Boolean
    Dim v As Variant
    For Each v In coll
        If StrComp(CStr(v), s, vbTextCompare) = 0 Then InColl = True: Exit Function
    Next v
    InColl = False
End Function


'============================================================================
'  GEOMETRY HELPERS
'============================================================================
Private Sub GetTBGeom(tb As Worksheet, codes As Object, ByRef hdr As Long, ByRef acct As Long, _
                      ByRef lastCol As Long, ByRef firstData As Long, ByRef lastData As Long)
    hdr = FindRowWithAnyKey(tb, codes): If hdr = 0 Then hdr = 6
    Dim gcn As Range: Set gcn = tb.Cells.Find("Group Account Number", , xlValues, xlWhole, , , False)
    If gcn Is Nothing Then acct = 1 Else acct = gcn.Column
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
    ' Locate the columns by their DATA, so it is immune to the number of columns
    ' and to the header wording:
    '   - the "From" currency column is the FIRST column whose cells are 3-letter
    '     currency codes (the "To" column, all "INR", comes later);
    '   - the rate column is the one headed like an exchange rate, or failing
    '     that the first column to the right holding decimal numbers (a date or
    '     a 1/100 "From Ratio" column is all integers, so it is skipped).
    Dim ur As Range: Set ur = rs.UsedRange
    Dim r0 As Long, c0 As Long, rN As Long, cN As Long, scanN As Long
    r0 = ur.Row: c0 = ur.Column
    rN = ur.Row + ur.Rows.Count - 1
    cN = ur.Column + ur.Columns.Count - 1
    scanN = r0 + 79: If scanN > rN Then scanN = rN

    Dim c As Long, r As Long
    fromCol = 0: hdr = 0
    For c = c0 To cN
        For r = r0 To scanN
            If IsCurrencyCode(rs.Cells(r, c).Value) Then
                fromCol = c
                hdr = r - 1: If hdr < 1 Then hdr = 1
                Exit For
            End If
        Next r
        If fromCol > 0 Then Exit For
    Next c
    If fromCol = 0 Then fromCol = 3
    If hdr = 0 Then hdr = 1

    rateCol = 0
    For c = fromCol To cN
        If IsRateHeader(rs.Cells(hdr, c).Value) Then rateCol = c: Exit For
    Next c
    If rateCol = 0 Then
        For c = fromCol + 1 To cN
            If ColumnHasDecimals(rs, hdr + 1, scanN, c) Then rateCol = c: Exit For
        Next c
    End If
    If rateCol = 0 Then rateCol = fromCol + 3
End Sub

' True for a 3-letter currency code (CHF, INR, USD ...) - text only.
Private Function IsCurrencyCode(ByVal v As Variant) As Boolean
    If IsError(v) Then IsCurrencyCode = False: Exit Function
    Dim s As String: s = Trim$(CStr(v))
    If Len(s) <> 3 Then IsCurrencyCode = False: Exit Function
    Dim i As Long, ch As Long
    For i = 1 To 3
        ch = Asc(UCase$(Mid$(s, i, 1)))
        If ch < 65 Or ch > 90 Then IsCurrencyCode = False: Exit Function
    Next i
    IsCurrencyCode = True
End Function

' True for an exchange-rate header, excluding "From Ratio" / "Rate Type".
Private Function IsRateHeader(ByVal v As Variant) As Boolean
    Dim s As String: s = NormHdr(CStr(v))
    If Len(s) = 0 Then IsRateHeader = False: Exit Function
    If InStr(s, "ratio") > 0 Or InStr(s, "type") > 0 Then IsRateHeader = False: Exit Function
    IsRateHeader = (s = "exchange rate" Or s = "exch rate" Or s = "rate" Or InStr(s, "exch") > 0)
End Function

' lower-cased, dot-stripped, space-collapsed header text
Private Function NormHdr(ByVal s As String) As String
    Dim t As String: t = LCase$(Trim$(Replace(s, ".", " ")))
    Do While InStr(t, "  ") > 0: t = Replace(t, "  ", " "): Loop
    NormHdr = t
End Function

' True if the column holds a non-integer number (a rate, not a date / ratio).
Private Function ColumnHasDecimals(rs As Worksheet, ByVal r1 As Long, ByVal r2 As Long, ByVal c As Long) As Boolean
    Dim r As Long, v As Variant
    For r = r1 To r2
        v = rs.Cells(r, c).Value
        If IsNumeric(v) Then
            If CDbl(v) <> Int(CDbl(v)) Then ColumnHasDecimals = True: Exit Function
        End If
    Next r
    ColumnHasDecimals = False
End Function

' Locate the tracker's join key + latest-month value columns, by header/data so
' it is immune to column count / wording (mirrors the Python parse_consol).
Private Sub GetConsolGeom(cs As Worksheet, ByRef concatCol As Long, ByRef val1 As Long, _
                          ByRef val2 As Long, ByRef firstRow As Long, ByRef lastRow As Long, _
                          ByRef funcCol As Long)
    Dim ur As Range: Set ur = cs.UsedRange
    Dim r0 As Long, c0 As Long, rN As Long, cN As Long
    r0 = ur.Row: c0 = ur.Column
    rN = ur.Row + ur.Rows.Count - 1
    cN = ur.Column + ur.Columns.Count - 1

    ' 'Concatenate' header -> join-key column + header row
    Dim hdr As Long, r As Long, c As Long: hdr = 0: concatCol = 0
    Dim scanN As Long: scanN = r0 + 39: If scanN > rN Then scanN = rN
    For r = r0 To scanN
        For c = c0 To cN
            If NormHdr(CStr(cs.Cells(r, c).Value)) = "concatenate" Then
                concatCol = c: hdr = r: Exit For
            End If
        Next c
        If concatCol > 0 Then Exit For
    Next r
    If concatCol = 0 Then concatCol = 3: hdr = 1     ' fallback (column C)

    ' right edge of the value area = just left of Currency / Type
    Dim boundary As Long: boundary = cN
    Dim h As String
    funcCol = 0
    For c = concatCol + 1 To cN
        h = NormHdr(CStr(cs.Cells(hdr, c).Value))
        If h = "currency" Or h = "type" Then boundary = c - 1: Exit For
    Next c
    ' the 'Functional Group' column (COS / S&M / G&A), if present
    For c = c0 To cN
        h = NormHdr(CStr(cs.Cells(hdr, c).Value))
        If h = "functional group" Or h = "function group" Or h = "functional grp" Then
            funcCol = c: Exit For
        End If
    Next c

    ' latest month's Dr/Cr pair = the two right-most columns of the value area
    If boundary - 1 > concatCol Then
        val1 = boundary - 1: val2 = boundary
    Else
        val1 = boundary: val2 = 0
    End If

    firstRow = hdr + 1
    lastRow = hdr
    For r = hdr + 1 To rN
        If Len(Trim$(CStr(cs.Cells(r, concatCol).Value))) > 0 Then lastRow = r
    Next r
    If lastRow < firstRow Then lastRow = firstRow
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
    Dim f As Range: Set f = ag.Cells.Find("Company", , xlValues, xlWhole, , , False)
    If f Is Nothing Then FindAggSubRow = 0 Else FindAggSubRow = f.Row
End Function


'============================================================================
'  CONFIG: section name -> classification / source  (edit here if names differ)
'============================================================================
Private Function CatClass(ByVal cat As String) As String
    ' Minority Interest nets out and is excluded from the P&L. Matched fuzzily
    ' (see IsMinority) so a typo like "Mintority Interest" is still excluded,
    ' not swept into the Nature-wise expense fallback below.
    If IsMinority(cat) Then CatClass = "": Exit Function
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
    ' Net Profit and Minority Interest are summary lines, not GL detail: they
    ' have NO LC tie-out. Guard them here so the Nature-wise fallback below
    ' (which ties everything else to the TB) can't put a bogus TB lookup on the
    ' Net Profit GL row.
    If IsMinority(cat) Then CatSource = "": Exit Function
    If StrComp(Trim$(cat), "Net Profit", vbTextCompare) = 0 Then CatSource = "": Exit Function
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

' The consol-tracker Functional Group code for a report section (COS/S&M/G&A),
' or "" when the row isn't a functional split (then match account-only). Reuses
' the section -> Aggregate-block mapping, so it follows CatSource automatically.
Private Function FuncCode(ByVal cat As String) As String
    Select Case CatSource(cat)
        Case "Cost of revenue": FuncCode = "COS"
        Case "Sales & Marketing": FuncCode = "S&M"
        Case "General Administration": FuncCode = "G&A"
        Case Else: FuncCode = ""
    End Select
End Function

' ---- fuzzy "Minority Interest" matching (mirrors the Python tool) -----------
' True if cat names a Minority-Interest section, tolerant of small typos
' (e.g. "Mintority Interest" / "Minority  Interest"). A 0.82 similarity accepts
' a one/two-character slip but still rejects unrelated section names.
Private Function IsMinority(ByVal cat As String) As Boolean
    Dim aliases As Variant
    aliases = Array("minority interest", "minority interests", _
                    "non-controlling interest", "non controlling interest")
    Dim n As String: n = NormCat(cat)
    If Len(n) = 0 Then IsMinority = False: Exit Function
    Dim i As Long
    For i = LBound(aliases) To UBound(aliases)
        If SimRatio(n, CStr(aliases(i))) >= 0.82 Then
            IsMinority = True: Exit Function
        End If
    Next i
    IsMinority = False
End Function

' whitespace-collapsed, lower-cased key for tolerant name matching
Private Function NormCat(ByVal s As String) As String
    Dim t As String: t = LCase$(Trim$(s))
    Do While InStr(t, "  ") > 0
        t = Replace(t, "  ", " ")
    Loop
    NormCat = t
End Function

' normalized similarity in [0,1] = 1 - Levenshtein / longer-length
Private Function SimRatio(ByVal a As String, ByVal b As String) As Double
    Dim m As Long: m = Len(a): If Len(b) > m Then m = Len(b)
    If m = 0 Then SimRatio = 1: Exit Function
    SimRatio = 1# - (Lev(a, b) / m)
End Function

' Levenshtein edit distance
Private Function Lev(ByVal a As String, ByVal b As String) As Long
    Dim la As Long, lb As Long: la = Len(a): lb = Len(b)
    If la = 0 Then Lev = lb: Exit Function
    If lb = 0 Then Lev = la: Exit Function
    Dim d() As Long: ReDim d(0 To la, 0 To lb)
    Dim i As Long, j As Long
    For i = 0 To la: d(i, 0) = i: Next i
    For j = 0 To lb: d(0, j) = j: Next j
    For i = 1 To la
        For j = 1 To lb
            Dim cost As Long
            If Mid$(a, i, 1) = Mid$(b, j, 1) Then cost = 0 Else cost = 1
            Dim x As Long, y As Long, z As Long
            x = d(i - 1, j) + 1
            y = d(i, j - 1) + 1
            z = d(i - 1, j - 1) + cost
            If y < x Then x = y
            If z < x Then x = z
            d(i, j) = x
        Next j
    Next i
    Lev = d(la, lb)
End Function

' ---- group-currency auto-detection (mirrors the Python tool) ----------------
Private Function RatesDict(rs As Worksheet) As Object
    Dim d As Object: Set d = CreateObject("Scripting.Dictionary")
    Dim fromCol As Long, rateCol As Long, hdr As Long
    GetRatesGeom rs, fromCol, rateCol, hdr
    Dim r As Long, lastR As Long
    lastR = rs.UsedRange.Row + rs.UsedRange.Rows.Count - 1
    For r = hdr + 1 To lastR
        Dim cur As String: cur = UCase$(Trim$(CStr(rs.Cells(r, fromCol).Value)))
        Dim v As Variant: v = rs.Cells(r, rateCol).Value
        If Len(cur) > 0 And IsNumeric(v) Then
            If Not d.Exists(cur) Then d(cur) = CDbl(v)
        End If
    Next r
    Set RatesDict = d
End Function

' GC-Balance = (LC + Consol) * local->INR / GC->INR, so the implied GC->INR rate
' is (LC+Consol)*localRate/GC; the currency in the table that matches it wins.
Private Function DetectGc(r As Worksheet, rData0 As Long, lastRRow As Long, _
                          entOrder As Collection, ccy As Object, repCol As Object, _
                          ratesD As Object) As String
    Dim votes As Object: Set votes = CreateObject("Scripting.Dictionary")
    Dim i As Long, rr As Long
    For i = 1 To entOrder.Count
        Dim code As String: code = entOrder(i)
        Dim cur As String: cur = UCase$(Trim$(CStr(ccy(code))))
        If ratesD.Exists(cur) And repCol.Exists("LC - Balance|" & code) _
           And repCol.Exists("GC - Balance|" & code) Then
            Dim lcCol As Long, gcCol As Long, lcoCol As Long
            lcCol = repCol("LC - Balance|" & code)
            gcCol = repCol("GC - Balance|" & code)
            lcoCol = 0
            If repCol.Exists("LC - Consol|" & code) Then lcoCol = repCol("LC - Consol|" & code)
            For rr = rData0 To lastRRow
                Dim lc As Double, gc As Double
                lc = Val0(r.Cells(rr, lcCol).Value)
                If lcoCol > 0 Then lc = lc + Val0(r.Cells(rr, lcoCol).Value)
                gc = Val0(r.Cells(rr, gcCol).Value)
                If lc <> 0 And gc <> 0 Then
                    Dim implied As Double: implied = lc * ratesD(cur) / gc
                    Dim best As String, bestErr As Double, k As Variant
                    best = "": bestErr = 1E+99
                    For Each k In ratesD.Keys
                        If ratesD(k) <> 0 Then
                            Dim rel As Double: rel = Abs(ratesD(k) - implied) / ratesD(k)
                            If rel < bestErr Then bestErr = rel: best = CStr(k)
                        End If
                    Next k
                    If best <> "" And bestErr < 0.01 Then
                        If Not votes.Exists(best) Then votes(best) = 0
                        votes(best) = votes(best) + 1
                    End If
                End If
            Next rr
        End If
    Next i
    Dim win As String, winN As Long, k2 As Variant
    win = "INR": winN = 0
    For Each k2 In votes.Keys
        If votes(k2) > winN Then winN = votes(k2): win = CStr(k2)
    Next k2
    DetectGc = win
End Function

Private Function Val0(ByVal v As Variant) As Double
    If IsNumeric(v) Then Val0 = CDbl(v)
End Function

Private Function SheetExists(wb As Workbook, ByVal nm As String) As Boolean
    Dim ws As Worksheet
    On Error Resume Next
    Set ws = wb.Worksheets(nm)
    On Error GoTo 0
    SheetExists = Not ws Is Nothing
End Function

' Normalise a block label to its canonical form, ignoring spacing/case, so
' "GC-Total" / "gc - total" both match "GC - Total" (parity with the Python tool).
Private Function CanonicalBlock(ByVal label As String) As String
    Dim n As String: n = Replace(LCase$(Trim$(label)), " ", "")
    Select Case n
        Case "lc-balance": CanonicalBlock = "LC - Balance"
        Case "lc-consol": CanonicalBlock = "LC - Consol"
        Case "gc-balance": CanonicalBlock = "GC - Balance"
        Case "gc-reclass": CanonicalBlock = "GC - Reclass"
        Case "gc-elimination": CanonicalBlock = "GC - Elimination"
        Case "gc-consol": CanonicalBlock = "GC - Consol"
        Case "gc-total": CanonicalBlock = "GC - Total"
        Case Else: CanonicalBlock = Trim$(label)
    End Select
End Function

' True if the GL account is a P&L-series account (leading digit 1/2/3).
Private Function IsPlAcct(ByVal acc As String) As Boolean
    Dim s As String: s = Trim$(acc)
    Do While Len(s) > 0 And Left$(s, 1) = "-"
        s = Mid$(s, 2)
    Loop
    If Len(s) = 0 Then IsPlAcct = False: Exit Function
    IsPlAcct = (InStr(PL_DIGITS, Left$(s, 1)) > 0)
End Function

Private Function IsCheckedBlock(ByVal blk As String) As Boolean
    Select Case LCase$(Trim$(blk))
        Case "lc - balance", "gc - balance", "gc - total": IsCheckedBlock = True
        ' LC - Consol is reconciled on its own dedicated sheet, not here, so the
        ' Check tab keeps only its value columns (needed for the FX base).
        Case Else: IsCheckedBlock = False
    End Select
End Function


'============================================================================
'  SMALL HELPERS
'============================================================================
' True for a real company code: short, alphanumeric, no spaces - so a dimension
' label like "Consolidation unit" (has a space) is rejected.
Private Function IsEntityCode(ByVal v As String) As Boolean
    Dim s As String: s = Trim$(v)
    ' known label words, tolerant of case AND spacing (spaces removed first)
    Select Case LCase$(Replace(s, " ", ""))
        Case "company", "overallresult", "groupaccountnumber", _
             "consolidationunit", "group", "total", "result", "unit": Exit Function
    End Select
    If Len(s) < 2 Or Len(s) > 15 Then Exit Function
    If InStr(s, " ") > 0 Then Exit Function
    Dim i As Long, ch As Long
    For i = 1 To Len(s)
        ch = Asc(UCase$(Mid$(s, i, 1)))
        If Not ((ch >= 65 And ch <= 90) Or (ch >= 48 And ch <= 57)) Then Exit Function
    Next i
    IsEntityCode = True
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
    Dim f As Range: Set f = ws.Cells.Find(txt, , xlValues, xlWhole, , , False)
    If f Is Nothing Then FindRowContaining = 0 Else FindRowContaining = f.Row
End Function

Private Function FindRowWithAnyKey(ws As Worksheet, codes As Object) As Long
    Dim k As Variant, f As Range
    For Each k In codes.Keys
        Set f = ws.Cells.Find(CStr(k), , xlValues, xlWhole, , , False)
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

' Import the tracker's data sheet (the one with a 'Concatenate' header), since
' it may not be the first tab in the source workbook.
Private Sub ImportConsolSheet(wb As Workbook, ByVal path As String, ByVal newName As String)
    Dim src As Workbook
    Set src = Workbooks.Open(Filename:=path, ReadOnly:=True, UpdateLinks:=0)
    Dim sh As Worksheet, pick As Worksheet: Set pick = Nothing
    Dim r As Long, c As Long, rMax As Long, cMax As Long, found As Boolean
    For Each sh In src.Worksheets
        found = False
        rMax = sh.UsedRange.Row + sh.UsedRange.Rows.Count - 1
        cMax = sh.UsedRange.Column + sh.UsedRange.Columns.Count - 1
        If rMax > 40 Then rMax = 40
        If cMax > 40 Then cMax = 40
        For r = 1 To rMax
            For c = 1 To cMax
                If NormHdr(CStr(sh.Cells(r, c).Value)) = "concatenate" Then found = True: Exit For
            Next c
            If found Then Exit For
        Next r
        If found Then Set pick = sh: Exit For
    Next sh
    If pick Is Nothing Then Set pick = src.Worksheets(1)
    pick.Copy After:=wb.Worksheets(wb.Worksheets.Count)
    wb.Worksheets(wb.Worksheets.Count).Name = newName
    src.Close SaveChanges:=False
End Sub
