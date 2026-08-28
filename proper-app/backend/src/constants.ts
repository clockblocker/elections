export const ELECTION_SLUG = "2021-duma";

export const ACCOUNTING_KEYS = {
  registeredVoters: "Число избирателей, внесенных в список избирателей на момент окончания голосования",
  ballotsReceived: "Число избирательных бюллетеней, полученных участковой избирательной комиссией",
  ballotsIssuedEarly: "Число избирательных бюллетеней, выданных избирателям, проголосовавшим досрочно",
  ballotsIssuedAtStation: "Число избирательных бюллетеней, выданных в помещении для голосования в день голосования",
  ballotsIssuedOutside: "Число избирательных бюллетеней, выданных вне помещения для голосования в день голосования",
  ballotsCancelled: "Число погашенных избирательных бюллетеней",
  portableBoxBallots: "Число избирательных бюллетеней, содержащихся в переносных ящиках для голосования",
  stationaryBoxBallots: "Число избирательных бюллетеней, содержащихся в стационарных ящиках для голосования",
  invalidBallots: "Число недействительных избирательных бюллетеней",
  validBallots: "Число действительных избирательных бюллетеней",
  lostBallots: "Число утраченных избирательных бюллетеней",
  unaccountedBallots: "Число избирательных бюллетеней, не учтенных при получении"
} as const;

export const PARTY_STYLE: Readonly<Record<number, { shortName: string; color: string }>> = {
  1: { shortName: "КПРФ", color: "#d54b3d" },
  2: { shortName: "Зелёные", color: "#4d8766" },
  3: { shortName: "ЛДПР", color: "#437fc7" },
  4: { shortName: "Новые люди", color: "#27a6a1" },
  5: { shortName: "Единая Россия", color: "#315f9d" },
  6: { shortName: "СРЗП", color: "#d09a30" },
  7: { shortName: "Яблоко", color: "#6ca84f" },
  8: { shortName: "Партия Роста", color: "#c78554" },
  9: { shortName: "РПСС", color: "#8b6c9d" },
  10: { shortName: "Коммунисты России", color: "#982f36" },
  11: { shortName: "Гражданская Платформа", color: "#686f79" },
  12: { shortName: "Зелёная Альтернатива", color: "#70a897" },
  13: { shortName: "Родина", color: "#9b8050" },
  14: { shortName: "Партия пенсионеров", color: "#9a6a7a" }
};
